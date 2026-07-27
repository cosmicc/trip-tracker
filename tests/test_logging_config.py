import logging
import sys

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings
from trip_tracker.logging_config import (
    LocalTimezoneFormatter,
    configure_logging,
    log_level_value,
)


def test_log_level_normalizes_supported_values() -> None:
    settings = Settings(log_level="WARNING")

    assert settings.log_level == "warning"
    assert log_level_value(settings) == logging.WARNING


def test_log_level_rejects_error_as_configured_level() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="error")


def test_report_display_name_trims_whitespace() -> None:
    settings = Settings(report_display_name="  Jane Technician  ")

    assert settings.report_display_name == "Jane Technician"


def test_web_login_requires_complete_credentials() -> None:
    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD"):
        Settings(web_login_username="admin")

    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD"):
        Settings(web_login_password="secret-password")


def test_web_login_rejects_default_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(
            web_login_username="admin",
            web_login_password="secret-password",
        )


def test_production_requires_web_login_and_changed_secret_key() -> None:
    with pytest.raises(ValidationError, match="WEB_LOGIN_USERNAME"):
        Settings(
            app_env="production",
            secret_key="production-test-secret",
        )

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(
            app_env="production",
            secret_key="change-me",
            web_login_username="admin",
            web_login_password="secret-password",
        )

    settings = Settings(
        app_env="production",
        secret_key="production-test-secret",
        web_login_username="admin",
        web_login_password="secret-password",
        web_api_key="web-api-secret",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
    )

    assert settings.app_env == "production"


def test_formatter_adds_level_to_exception_traceback_lines() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    try:
        raise RuntimeError("sample failure")
    except RuntimeError:
        record = logging.LogRecord(
            "trip_tracker.test",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            sys.exc_info(),
        )

    lines = formatter.format(record).splitlines()

    assert len(lines) > 1
    assert all(" ERROR [trip_tracker.test] " in line for line in lines)


def test_formatter_redacts_sensitive_query_values() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    record = logging.LogRecord(
        "trip_tracker.test",
        logging.INFO,
        __file__,
        1,
        "GET https://api.example.test/path?api_key=secret-value&series=test",
        (),
        None,
    )

    formatted = formatter.format(record)

    assert "api_key=***" in formatted
    assert "secret-value" not in formatted


def test_formatter_redacts_bearer_tokens() -> None:
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    record = logging.LogRecord(
        "trip_tracker.test",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer secret-provider-token",
        (),
        None,
    )

    formatted = formatter.format(record)

    assert "Authorization: Bearer ***" in formatted
    assert "secret-provider-token" not in formatted


def test_configure_logging_uses_console_without_file_handlers(monkeypatch) -> None:
    settings = Settings(log_level="debug")
    monkeypatch.setattr("trip_tracker.logging_config.get_settings", lambda: settings)

    configure_logging("test-console")

    root_logger = logging.getLogger()
    handlers = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "_trip_tracker_marker", "")
        == "trip_tracker_test-console_console"
    ]
    try:
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.StreamHandler)
        assert not isinstance(handlers[0], logging.FileHandler)
        assert handlers[0].stream is sys.stdout
    finally:
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()


def test_runtime_state_defaults_under_app_data_directory() -> None:
    settings = Settings(app_data_dir="/data")

    assert settings.automatic_backup_dir == "/data/backups"
    assert settings.automatic_backup_retry_seconds == 60
    assert settings.app_health_state_path == "/data/app-health-state.json"


def test_disk_free_space_thresholds_require_critical_at_or_below_warning() -> None:
    with pytest.raises(ValidationError, match="APP_HEALTH_DISK_CRITICAL_FREE_MB"):
        Settings(
            app_health_disk_warning_free_mb=250,
            app_health_disk_critical_free_mb=1000,
        )
