import logging
import re
import sys
from datetime import datetime

from trip_tracker.config import Settings, get_settings
from trip_tracker.services.timezone import local_timezone

TRIP_CALCULATION_LOGGER = "trip_tracker.trip_calculation"
SENSITIVE_QUERY_VALUE_RE = re.compile(
    r"(?i)(\b(?:api_key|apikey|access_token|refresh_token|token|client_secret|password)=)"
    r"([^&\s\"']+)"
)
SENSITIVE_BEARER_VALUE_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s\"']+)")
LOG_LEVEL_VALUES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
}


def redact_sensitive_text(value: str) -> str:
    redacted = SENSITIVE_QUERY_VALUE_RE.sub(r"\1***", value)
    return SENSITIVE_BEARER_VALUE_RE.sub(r"\1***", redacted)


class LocalTimezoneFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if "\n" not in formatted:
            return redact_sensitive_text(formatted)
        line_prefix = f"{self.formatTime(record, self.datefmt)} {record.levelname} [{record.name}] "
        lines = formatted.splitlines()
        prefixed = "\n".join([lines[0], *(f"{line_prefix}{line}" for line in lines[1:])])
        return redact_sensitive_text(prefixed)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        value = datetime.fromtimestamp(record.created, tz=local_timezone())
        if datefmt:
            return value.strftime(datefmt)
        return value.isoformat(timespec="seconds")


def log_level_value(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return LOG_LEVEL_VALUES[settings.log_level]


def configure_logging(process_name: str) -> None:
    """Configure application logging on standard output for container collection."""

    settings = get_settings()
    level = log_level_value(settings)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))
    logging.getLogger("requests").setLevel(max(level, logging.WARNING))
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))

    marker = f"trip_tracker_{process_name}_console"
    formatter = LocalTimezoneFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )

    trip_logger = logging.getLogger(TRIP_CALCULATION_LOGGER)
    trip_logger.setLevel(level)
    trip_logger.propagate = True

    for handler in root_logger.handlers:
        if getattr(handler, "_trip_tracker_marker", "") == marker:
            handler.setLevel(level)
            handler.setFormatter(formatter)
            return

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler._trip_tracker_marker = marker  # type: ignore[attr-defined]
    root_logger.addHandler(console_handler)
