from decimal import Decimal
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["debug", "info", "warning"]
PRODUCTION_ENVIRONMENTS = {"prod", "production"}
UNSAFE_SECRET_KEYS = {"", "change-me"}


def _is_blank(value: str) -> bool:
    return not value.strip()


def _secret_key_is_unsafe(value: str) -> bool:
    return value.strip().casefold() in UNSAFE_SECRET_KEYS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Trip Tracker"
    app_env: str = "local"
    secret_key: str = "change-me"
    local_timezone: str = "America/Detroit"
    database_url: str = (
        "postgresql+psycopg://triptracker:triptracker@postgres:5432/trip_tracker"
    )
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout_seconds: int = Field(default=30, ge=1)
    database_pool_recycle_seconds: int = Field(default=1800, ge=1)
    database_connect_timeout_seconds: int = Field(default=10, ge=1)
    create_tables_on_startup: bool = False
    web_login_username: str = ""
    web_login_password: str = ""
    web_session_cookie_secure: bool = False
    web_login_max_attempts: int = Field(default=5, ge=1)
    web_login_lockout_seconds: int = Field(default=300, ge=1)
    passkey_rp_name: str = "Trip Tracker"
    passkey_rp_id: str = ""
    passkey_origin: str = ""
    cloudflare_ip_blocking_enabled: bool = False
    cloudflare_api_token: str = ""
    cloudflare_zone_id: str = ""
    cloudflare_ip_block_allowlist: str = ""
    cloudflare_auto_block_failed_login_attempts: int = Field(default=5, ge=1)

    pushover_enabled: bool = False
    pushover_token: str = ""
    pushover_user: str = ""
    pushover_app_key: str = ""
    pushover_user_key: str = ""
    pushover_device: str = ""
    pushover_priority: int = Field(default=0, ge=-2, le=2)
    pushover_timeout_seconds: int = Field(default=10, ge=1)
    app_health_monitor_interval_seconds: int = Field(default=60, ge=15)
    app_health_reminder_interval_seconds: int = Field(default=3600, ge=60)
    app_health_db_latency_warning_ms: int = Field(default=500, ge=1)
    app_health_db_latency_critical_ms: int = Field(default=2000, ge=1)
    app_health_db_latency_sustained_seconds: int = Field(default=15, ge=1)
    app_health_disk_warning_free_mb: int = Field(default=1000, ge=1)
    app_health_disk_critical_free_mb: int = Field(default=250, ge=1)
    app_health_state_path: str = ""

    web_api_key: str = ""
    owntracks_username: str = ""
    owntracks_password: str = ""
    owntracks_encryption_key: str = ""
    owntracks_sync_waypoints: bool = True
    owntracks_default_site_radius_m: int = 150
    automatic_trip_processing_enabled: bool = True
    automatic_trip_processing_interval_seconds: int = Field(default=60, ge=5)
    owntracks_purge_enabled: bool = True
    owntracks_location_retention_days: int = Field(default=90, ge=1)
    owntracks_waypoint_dwell_minutes: int = Field(default=5, ge=1)
    owntracks_travel_distance_m: Decimal = Field(default=Decimal("50.0"), ge=Decimal("0"))
    gas_price_state: str = "MI"
    gas_price_buffer: Decimal = Decimal("0.50")
    gas_price_source: str = "aaa_current"
    eia_api_key: str = ""
    eia_series_id: str = ""
    vehicle_mpg: Decimal = Field(default=Decimal("25.0"), gt=Decimal("0"))
    report_display_name: str = Field(default="", max_length=160)
    gas_snapshot_enabled: bool = False
    gas_snapshot_interval_seconds: int = Field(default=86400, ge=60)
    gas_snapshot_run_on_startup: bool = True

    app_data_dir: str = "data"
    log_level: LogLevel = "info"
    max_backup_restore_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    automatic_backups_enabled: bool = True
    automatic_backup_dir: str = ""
    automatic_backup_retry_seconds: int = Field(default=60, ge=5)
    min_trip_miles: Decimal = Field(default=Decimal("0.10"), ge=Decimal("0"))

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> str:
        normalized = str(value or "info").strip().casefold()
        if normalized not in {"debug", "info", "warning"}:
            raise ValueError("LOG_LEVEL must be debug, info, or warning")
        return normalized

    @field_validator("passkey_rp_id", mode="before")
    @classmethod
    def validate_passkey_rp_id(cls, value: object) -> str:
        """Validate an optional WebAuthn relying-party ID."""

        rp_id = str(value or "").strip().lower()
        if not rp_id:
            return ""
        if "://" in rp_id or "/" in rp_id or ":" in rp_id:
            raise ValueError("PASSKEY_RP_ID must be a host name without scheme, port, or path")
        return rp_id

    @field_validator("passkey_origin", mode="before")
    @classmethod
    def validate_passkey_origin(cls, value: object) -> str:
        """Validate an optional WebAuthn origin override."""

        origin = str(value or "").strip().rstrip("/")
        if not origin:
            return ""
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PASSKEY_ORIGIN must be an http:// or https:// origin")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("PASSKEY_ORIGIN must not include a path, query, or fragment")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in local_hosts:
            raise ValueError("PASSKEY_ORIGIN must use https outside localhost testing")
        return origin

    @field_validator(
        "web_api_key",
        "owntracks_encryption_key",
        "pushover_token",
        "pushover_user",
        "pushover_app_key",
        "pushover_user_key",
        "pushover_device",
        mode="before",
    )
    @classmethod
    def strip_secret_text(cls, value: object) -> str:
        """Normalize optional shared-secret settings without logging or deriving them."""

        return str(value or "").strip()

    @field_validator("report_display_name", mode="before")
    @classmethod
    def normalize_report_display_name(cls, value: object) -> str:
        """Normalize the optional human-readable PDF report submitter name."""

        return str(value or "").strip()

    @field_validator("owntracks_encryption_key")
    @classmethod
    def validate_owntracks_encryption_key(cls, value: str) -> str:
        """Validate OwnTracks' libsodium shared secret size limit."""

        if len(value.encode("utf-8")) > 32:
            raise ValueError("OWNTRACKS_ENCRYPTION_KEY must be 32 UTF-8 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_web_security_settings(self) -> Self:
        """Fail closed for unsafe web authentication settings."""

        app_env = self.app_env.strip().casefold()
        username_configured = not _is_blank(self.web_login_username)
        password_configured = not _is_blank(self.web_login_password)
        web_login_configured = username_configured and password_configured
        if username_configured != password_configured:
            raise ValueError(
                "WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD must both be set or both be blank"
            )
        if web_login_configured and _secret_key_is_unsafe(self.secret_key):
            raise ValueError("SECRET_KEY must be changed before enabling web login")
        owntracks_basic_configured = bool(
            self.owntracks_username.strip() and self.owntracks_password.strip()
        )
        if self.owntracks_encryption_key and not owntracks_basic_configured:
            raise ValueError(
                "OWNTRACKS_USERNAME and OWNTRACKS_PASSWORD must be set when "
                "OWNTRACKS_ENCRYPTION_KEY is set"
            )
        if app_env in PRODUCTION_ENVIRONMENTS:
            if not web_login_configured:
                raise ValueError(
                    "WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD must be set when APP_ENV=production"
                )
            if _secret_key_is_unsafe(self.secret_key):
                raise ValueError("SECRET_KEY must be changed when APP_ENV=production")
            if not self.web_api_key.strip():
                raise ValueError("WEB_API_KEY must be set when APP_ENV=production")
            if not self.owntracks_encryption_key.strip():
                raise ValueError("OWNTRACKS_ENCRYPTION_KEY must be set when APP_ENV=production")
            if not owntracks_basic_configured:
                raise ValueError(
                    "OWNTRACKS_USERNAME and OWNTRACKS_PASSWORD must be set when "
                    "APP_ENV=production"
                )
        return self

    @model_validator(mode="after")
    def validate_app_health_thresholds(self) -> Self:
        """Validate app-health monitor thresholds and Pushover aliases."""

        if self.app_health_db_latency_warning_ms > self.app_health_db_latency_critical_ms:
            raise ValueError(
                "APP_HEALTH_DB_LATENCY_WARNING_MS must be less than or equal to "
                "APP_HEALTH_DB_LATENCY_CRITICAL_MS"
            )
        if (
            self.app_health_disk_critical_free_mb
            > self.app_health_disk_warning_free_mb
        ):
            raise ValueError(
                "APP_HEALTH_DISK_CRITICAL_FREE_MB must be less than or equal to "
                "APP_HEALTH_DISK_WARNING_FREE_MB"
            )
        return self

    @model_validator(mode="after")
    def default_automatic_backup_dir(self) -> Self:
        """Default persistent runtime state under the application data directory."""

        if not self.automatic_backup_dir.strip():
            self.automatic_backup_dir = f"{self.app_data_dir.rstrip('/')}/backups"
        if not self.app_health_state_path.strip():
            self.app_health_state_path = f"{self.app_data_dir.rstrip('/')}/app-health-state.json"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
