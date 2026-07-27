import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trip_tracker.config import Settings, get_settings
from trip_tracker.database import SessionLocal, is_database_unavailable_error
from trip_tracker.models import CloudflareIPBlock
from trip_tracker.services.runtime_status import RuntimeStatus, build_runtime_status
from trip_tracker.web.auth import FAILED_LOGIN_ATTEMPTS

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
SEVERITY_ORDER = {"ok": 0, "warning": 1, "critical": 2}
MEBIBYTE_BYTES = 1024 * 1024
DATABASE_LATENCY_THRESHOLD_ISSUE_KEYS = {
    "database.latency_warning",
    "database.latency_critical",
}


@dataclass(frozen=True)
class AppHealthIssue:
    """One current condition that degrades app availability or performance."""

    key: str
    severity: str
    title: str
    detail: str


@dataclass(frozen=True)
class AppHealthSnapshot:
    """Current app health summary for Diagnostics and notification state changes."""

    status: str
    severity: str
    issues: tuple[AppHealthIssue, ...]
    checked_at: datetime

    @property
    def is_degraded(self) -> bool:
        """Return true when any monitored app signal is warning or critical."""

        return bool(self.issues)

    @property
    def banner_class(self) -> str:
        """Return the Diagnostics banner class for this snapshot."""

        return "critical" if self.severity == "critical" else "warning"

    @property
    def banner_title(self) -> str:
        """Return a concise Diagnostics banner title."""

        if self.status == "unavailable":
            return "App Unavailable"
        return "App Degraded"

    @property
    def summary(self) -> str:
        """Return one readable sentence summarizing the current health state."""

        if not self.issues:
            return "All monitored checks are healthy."
        count = len(self.issues)
        suffix = "" if count == 1 else "s"
        return f"{count} monitored issue{suffix} detected."

    @property
    def notification_signature(self) -> str:
        """Return a stable issue signature that avoids repeated alert spam."""

        if not self.issues:
            return "ok"
        return "|".join(f"{issue.key}:{issue.severity}" for issue in self.issues)


@dataclass(frozen=True)
class HealthDiskUsage:
    """One logical disk usage row considered by app-health checks."""

    primary_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> Decimal:
        """Return used space as a percentage of total capacity."""

        if self.total_bytes <= 0:
            return Decimal("0")
        return (Decimal(self.used_bytes) / Decimal(self.total_bytes)) * Decimal("100")

    @property
    def used_percent_display(self) -> str:
        """Return a compact percentage label."""

        return f"{self.used_percent:.1f}%"


def pushover_api_token(settings: Settings) -> str:
    """Return the configured Pushover app token, including supported legacy aliases."""

    return settings.pushover_token.strip() or settings.pushover_app_key.strip()


def pushover_user_key(settings: Settings) -> str:
    """Return the configured Pushover user or group key."""

    return settings.pushover_user.strip() or settings.pushover_user_key.strip()


def pushover_configured(settings: Settings) -> bool:
    """Return whether Pushover notifications should be attempted."""

    return bool(
        settings.pushover_enabled
        and pushover_api_token(settings)
        and pushover_user_key(settings)
    )


def measure_database_latency_ms(db: Session) -> float | None:
    """Measure a lightweight database round trip for health and Diagnostics."""

    try:
        started_at = time.perf_counter()
        db.execute(text("SELECT 1"))
        return (time.perf_counter() - started_at) * 1000
    except SQLAlchemyError:
        logger.exception("Could not measure database latency")
        return None


def active_login_lockout_count() -> int:
    """Return the number of currently locked-out web-login client keys."""

    now = time.monotonic()
    return sum(1 for state in FAILED_LOGIN_ATTEMPTS.values() if state.locked_until > now)


def _sqlite_database_path(database_url: str) -> Path | None:
    """Return a local SQLite database path when the configured URL points to one."""

    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        parsed_url = make_url(database_url)
    except ArgumentError:
        return None
    if parsed_url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        return None
    database_path = parsed_url.database
    if not database_path or database_path == ":memory:":
        return None
    return Path(database_path)


def _health_storage_paths(settings: Settings) -> tuple[str, ...]:
    """Return configured paths that should be included in disk-health checks."""

    path_candidates = [
        str(Path.cwd()),
        settings.app_data_dir,
        settings.automatic_backup_dir,
    ]
    sqlite_path = _sqlite_database_path(settings.database_url)
    if sqlite_path is not None:
        path_candidates.append(str(sqlite_path))

    unique_paths: list[str] = []
    seen: set[str] = set()
    for path_text in path_candidates:
        cleaned_path = str(path_text).strip()
        if not cleaned_path or cleaned_path in seen:
            continue
        seen.add(cleaned_path)
        unique_paths.append(cleaned_path)
    return tuple(unique_paths)


def _existing_disk_usage_target(path: Path) -> Path | None:
    """Return the nearest existing path that can be passed to disk usage checks."""

    expanded_path = path.expanduser()
    candidate = expanded_path if expanded_path.is_absolute() else Path.cwd() / expanded_path
    if candidate.exists():
        return candidate
    for parent in candidate.parents:
        if parent.exists():
            return parent
    return None


def collect_health_disk_usages(
    settings: Settings,
    *,
    disk_usage_func: Callable[[Path], Any] = shutil.disk_usage,
) -> list[HealthDiskUsage]:
    """Return deduplicated disk usage rows for app-health checks."""

    grouped_paths: dict[tuple[int, int], list[tuple[str, str]]] = {}
    grouped_free_bytes: dict[tuple[int, int], int] = {}
    for path_text in _health_storage_paths(settings):
        target_path = _existing_disk_usage_target(Path(path_text))
        if target_path is None:
            continue
        try:
            usage = disk_usage_func(target_path)
        except OSError:
            logger.exception("Could not read disk usage for app health path=%s", path_text)
            continue
        key = (int(usage.used), int(usage.total))
        grouped_paths.setdefault(key, []).append((path_text, str(target_path)))
        grouped_free_bytes.setdefault(key, max(int(usage.free), 0))

    disk_usages: list[HealthDiskUsage] = []
    for (used_bytes, total_bytes), path_rows in grouped_paths.items():
        disk_usages.append(
            HealthDiskUsage(
                primary_path=path_rows[0][0],
                total_bytes=total_bytes,
                used_bytes=used_bytes,
                free_bytes=grouped_free_bytes[(used_bytes, total_bytes)],
            )
        )
    return sorted(disk_usages, key=lambda item: item.primary_path)


def _disk_severity_from_free_bytes(settings: Settings, free_bytes: int) -> str | None:
    """Classify disk health using configured free-space thresholds."""

    critical_bytes = settings.app_health_disk_critical_free_mb * MEBIBYTE_BYTES
    warning_bytes = settings.app_health_disk_warning_free_mb * MEBIBYTE_BYTES
    if free_bytes < critical_bytes:
        return "critical"
    if free_bytes < warning_bytes:
        return "warning"
    return None


def _format_free_mebibytes(free_bytes: int) -> str:
    """Return a readable free-space value for app-health details."""

    free_mebibytes = Decimal(free_bytes) / Decimal(MEBIBYTE_BYTES)
    return f"{free_mebibytes:,.1f} MiB"


def _runtime_status_issues(runtime_status: RuntimeStatus) -> list[AppHealthIssue]:
    """Return health issues from the current database status."""

    issues: list[AppHealthIssue] = []
    if not runtime_status.database.available:
        issues.append(
            AppHealthIssue(
                key="database.unavailable",
                severity="critical",
                title="PostgreSQL unavailable",
                detail="The configured database is not accepting app queries.",
            )
        )

    return issues


def _database_latency_issue(
    settings: Settings,
    database_latency_ms: float | None,
) -> AppHealthIssue | None:
    """Return a database latency issue when the measured round trip is degraded."""

    if database_latency_ms is None:
        return AppHealthIssue(
            key="database.latency_unavailable",
            severity="warning",
            title="Database latency unavailable",
            detail="The app could not measure database latency.",
        )
    if database_latency_ms >= settings.app_health_db_latency_critical_ms:
        return AppHealthIssue(
            key="database.latency_critical",
            severity="critical",
            title="Database latency critical",
            detail=f"Database round trip is {database_latency_ms:.1f} ms.",
        )
    if database_latency_ms >= settings.app_health_db_latency_warning_ms:
        return AppHealthIssue(
            key="database.latency_warning",
            severity="warning",
            title="Database latency elevated",
            detail=f"Database round trip is {database_latency_ms:.1f} ms.",
        )
    return None


def build_app_health_snapshot(
    *,
    settings: Settings | None = None,
    runtime_status: RuntimeStatus,
    database_latency_ms: float | None = None,
    disk_usages: list[Any] | None = None,
    active_lockout_count: int | None = None,
    cloudflare_block_count: int | None = None,
    extra_issues: list[AppHealthIssue] | None = None,
) -> AppHealthSnapshot:
    """Build a current health snapshot from shared Diagnostics and runtime signals."""

    active_settings = settings or get_settings()
    issues = _runtime_status_issues(runtime_status)
    if runtime_status.database.available:
        latency_issue = _database_latency_issue(active_settings, database_latency_ms)
        if latency_issue is not None:
            issues.append(latency_issue)

    for disk in disk_usages or []:
        total_bytes = int(getattr(disk, "total_bytes", 0) or 0)
        used_bytes = int(getattr(disk, "used_bytes", 0) or 0)
        if total_bytes <= 0:
            continue
        reported_free_bytes = getattr(disk, "free_bytes", None)
        free_bytes = (
            max(int(reported_free_bytes), 0)
            if reported_free_bytes is not None
            else max(total_bytes - used_bytes, 0)
        )
        severity = _disk_severity_from_free_bytes(active_settings, free_bytes)
        if severity is None:
            continue
        issues.append(
            AppHealthIssue(
                key=f"disk.{getattr(disk, 'primary_path', 'storage')}",
                severity=severity,
                title="Disk space low" if severity == "warning" else "Disk space critical",
                detail=(
                    f"{getattr(disk, 'primary_path', 'Storage')} is "
                    f"low on space with {_format_free_mebibytes(free_bytes)} free."
                ),
            )
        )

    lockout_count = active_lockout_count if active_lockout_count is not None else (
        active_login_lockout_count()
    )
    if lockout_count > 0:
        issues.append(
            AppHealthIssue(
                key="security.login_lockout",
                severity="warning",
                title="Web-login lockout active",
                detail=f"{lockout_count:,} client IPs are currently locked out.",
            )
        )

    if cloudflare_block_count:
        issues.append(
            AppHealthIssue(
                key="security.cloudflare_blocks",
                severity="warning",
                title="Cloudflare blocks active",
                detail=f"{cloudflare_block_count:,} app-managed IP blocks are active.",
            )
        )

    issues.extend(extra_issues or [])
    sorted_issues = tuple(sorted(issues, key=lambda item: (item.key, item.severity)))
    severity = "ok"
    if sorted_issues:
        severity = max(sorted_issues, key=lambda item: SEVERITY_ORDER[item.severity]).severity
    status = "ok"
    if sorted_issues:
        database_unavailable = any(item.key == "database.unavailable" for item in sorted_issues)
        status = "unavailable" if database_unavailable else "degraded"
    return AppHealthSnapshot(
        status=status,
        severity=severity,
        issues=sorted_issues,
        checked_at=datetime.now(UTC),
    )


def collect_app_health_snapshot(
    settings: Settings | None = None,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> AppHealthSnapshot:
    """Collect a full app-health snapshot for the background monitor."""

    active_settings = settings or get_settings()
    database_available = True
    database_latency_ms: float | None = None
    cloudflare_block_count: int | None = None
    extra_issues: list[AppHealthIssue] = []
    try:
        with session_factory() as db:
            database_latency_ms = measure_database_latency_ms(db)
            cloudflare_block_count = int(db.scalar(select(func.count(CloudflareIPBlock.id))) or 0)
    except Exception as exc:
        if is_database_unavailable_error(exc):
            database_available = False
        else:
            logger.exception("App health database check failed")
            extra_issues.append(
                AppHealthIssue(
                    key="health_monitor.database_check_failed",
                    severity="critical",
                    title="Health check failed",
                    detail="The app health monitor could not complete its database checks.",
                )
            )

    runtime_status = build_runtime_status(active_settings, database_available=database_available)
    return build_app_health_snapshot(
        settings=active_settings,
        runtime_status=runtime_status,
        database_latency_ms=database_latency_ms,
        disk_usages=collect_health_disk_usages(active_settings),
        cloudflare_block_count=cloudflare_block_count,
        extra_issues=extra_issues,
    )


def send_pushover_message(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: int | None = None,
) -> None:
    """Send one Pushover notification without logging secret values."""

    token = pushover_api_token(settings)
    user_key = pushover_user_key(settings)
    if not settings.pushover_enabled:
        return
    if not token or not user_key:
        logger.warning("Pushover is enabled but PUSHOVER_TOKEN/PUSHOVER_USER are not configured")
        return

    payload: dict[str, str | int] = {
        "token": token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": settings.pushover_priority if priority is None else priority,
    }
    if settings.pushover_device.strip():
        payload["device"] = settings.pushover_device.strip()

    response = httpx.post(
        PUSHOVER_API_URL,
        data=payload,
        timeout=settings.pushover_timeout_seconds,
    )
    response.raise_for_status()
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {}
    if response_payload and response_payload.get("status") not in {1, "1"}:
        raise RuntimeError("Pushover API rejected the notification request")


def _notification_message(snapshot: AppHealthSnapshot) -> str:
    """Return a concise notification message for one health snapshot."""

    if not snapshot.issues:
        return "Trip Tracker restored. All monitored checks are healthy."
    lines = [f"{issue.title}: {issue.detail}" for issue in snapshot.issues]
    return "\n".join(lines[:8])


def _notification_title(
    snapshot: AppHealthSnapshot,
    *,
    changed: bool,
    reminder: bool,
) -> str:
    """Return a title for degraded, unavailable, changed, reminder, or restored states."""

    if not snapshot.issues:
        return "Trip Tracker restored"
    if snapshot.status == "unavailable":
        suffix = " reminder" if reminder else ""
        return f"Trip Tracker unavailable{suffix}"
    if reminder:
        return "Trip Tracker degraded reminder"
    if changed:
        return "Trip Tracker degraded state changed"
    return "Trip Tracker degraded"


def _snapshot_with_issues(
    snapshot: AppHealthSnapshot,
    issues: tuple[AppHealthIssue, ...],
) -> AppHealthSnapshot:
    """Copy a snapshot while recalculating status for a filtered issue set."""

    severity = "ok"
    status = "ok"
    if issues:
        severity = max(issues, key=lambda item: SEVERITY_ORDER[item.severity]).severity
        status = (
            "unavailable"
            if any(issue.key == "database.unavailable" for issue in issues)
            else "degraded"
        )
    return AppHealthSnapshot(
        status=status,
        severity=severity,
        issues=issues,
        checked_at=snapshot.checked_at,
    )


class PushoverAppHealthNotifier:
    """Persist health state and send transition alerts plus recurring reminders."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.state_path = Path(self.settings.app_health_state_path)

    def notify_if_needed(self, snapshot: AppHealthSnapshot) -> bool:
        """Send Pushover state transitions and reminders for unresolved issues."""

        previous = self._read_state()
        previous_signature = str(previous.get("signature") or "")
        previous_status = str(previous.get("status") or "")
        current_signature = snapshot.notification_signature
        should_notify = False
        changed = False
        reminder = False
        last_notification_at = self._state_datetime(previous.get("last_notification_at"))
        if (
            last_notification_at is None
            and previous_status in {"degraded", "unavailable"}
        ):
            # Upgrade older state files without sending a duplicate alert immediately.
            last_notification_at = self._state_datetime(previous.get("updated_at"))

        if snapshot.issues:
            changed = bool(previous_signature and previous_signature != current_signature)
            state_changed = (
                previous_signature != current_signature
                or previous_status not in {"degraded", "unavailable"}
            )
            reminder = (
                not state_changed
                and (
                    last_notification_at is None
                    or (
                        snapshot.checked_at - last_notification_at
                    ).total_seconds()
                    >= self.settings.app_health_reminder_interval_seconds
                )
            )
            should_notify = state_changed or reminder
        elif previous_status in {"degraded", "unavailable"}:
            should_notify = True

        if should_notify:
            send_pushover_message(
                self.settings,
                title=_notification_title(
                    snapshot,
                    changed=changed,
                    reminder=reminder,
                ),
                message=_notification_message(snapshot),
                priority=1 if snapshot.severity == "critical" else self.settings.pushover_priority,
            )
            last_notification_at = snapshot.checked_at
        self._write_state(snapshot, last_notification_at=last_notification_at)
        return should_notify

    @staticmethod
    def _state_datetime(value: object) -> datetime | None:
        """Parse one persisted ISO timestamp as an aware UTC datetime."""

        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _read_state(self) -> dict[str, object]:
        """Read the persisted health notification state."""

        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _write_state(
        self,
        snapshot: AppHealthSnapshot,
        *,
        last_notification_at: datetime | None,
    ) -> None:
        """Persist the current health notification state."""

        payload = {
            "status": snapshot.status,
            "severity": snapshot.severity,
            "signature": snapshot.notification_signature,
            "updated_at": snapshot.checked_at.isoformat(),
            "last_notification_at": (
                last_notification_at.isoformat()
                if last_notification_at is not None
                else None
            ),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            logger.exception("Could not write app health notification state")


class AppHealthMonitor:
    """Background worker that checks app health and emits Pushover state changes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = Event()
        self._thread: Thread | None = None
        self._notifier = PushoverAppHealthNotifier(self.settings)
        self._latency_issue_started_at: float | None = None

    def start(self) -> None:
        """Start the monitor when Pushover notifications are enabled and configured."""

        if not self.settings.pushover_enabled:
            logger.info("Pushover app-health notifications are disabled")
            return
        if not pushover_configured(self.settings):
            logger.warning("Pushover enabled but token/user settings are incomplete")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="app-health-monitor", daemon=True)
        self._thread.start()
        logger.info(
            "Pushover app-health monitor started interval_seconds=%s "
            "reminder_interval_seconds=%s",
            self.settings.app_health_monitor_interval_seconds,
            self.settings.app_health_reminder_interval_seconds,
        )

    def stop(self) -> None:
        """Stop the background monitor."""

        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        logger.info("Pushover app-health monitor stopped")

    def check_once(self) -> AppHealthSnapshot:
        """Run one health check and notify on state changes."""

        snapshot = collect_app_health_snapshot(self.settings)
        notification_snapshot = self._notification_snapshot(snapshot)
        if notification_snapshot is None:
            return snapshot
        try:
            self._notifier.notify_if_needed(notification_snapshot)
        except Exception:
            logger.exception("Could not send app-health Pushover notification")
        return snapshot

    def _notification_snapshot(
        self,
        snapshot: AppHealthSnapshot,
    ) -> AppHealthSnapshot | None:
        """Suppress latency-only notifications until latency stays high long enough."""

        latency_issues = tuple(
            issue
            for issue in snapshot.issues
            if issue.key in DATABASE_LATENCY_THRESHOLD_ISSUE_KEYS
        )
        if not latency_issues:
            self._latency_issue_started_at = None
            return snapshot

        current_monotonic = time.monotonic()
        if self._latency_issue_started_at is None:
            self._latency_issue_started_at = current_monotonic
        elapsed_seconds = current_monotonic - self._latency_issue_started_at
        if elapsed_seconds >= self.settings.app_health_db_latency_sustained_seconds:
            return snapshot

        immediate_issues = tuple(
            issue
            for issue in snapshot.issues
            if issue.key not in DATABASE_LATENCY_THRESHOLD_ISSUE_KEYS
        )
        if not immediate_issues:
            return None
        return _snapshot_with_issues(snapshot, immediate_issues)

    def _next_check_delay(self) -> float:
        """Return the normal interval or the remaining latency confirmation delay."""

        normal_delay = float(self.settings.app_health_monitor_interval_seconds)
        if self._latency_issue_started_at is None:
            return normal_delay
        elapsed_seconds = time.monotonic() - self._latency_issue_started_at
        remaining_seconds = (
            self.settings.app_health_db_latency_sustained_seconds - elapsed_seconds
        )
        if remaining_seconds <= 0:
            return normal_delay
        return min(normal_delay, max(remaining_seconds, 0.1))

    def _run(self) -> None:
        """Run periodic health checks until stopped."""

        self.check_once()
        while not self._stop.wait(self._next_check_delay()):
            self.check_once()
