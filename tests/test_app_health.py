from datetime import UTC, datetime
from types import SimpleNamespace

from trip_tracker.config import Settings
from trip_tracker.services.app_health import (
    AppHealthIssue,
    AppHealthMonitor,
    AppHealthSnapshot,
    PushoverAppHealthNotifier,
    build_app_health_snapshot,
    pushover_configured,
)
from trip_tracker.services.runtime_status import (
    RuntimeDatabaseStatus,
    RuntimeStatus,
)


def _runtime_status(
    *,
    database_available: bool = True,
) -> RuntimeStatus:
    return RuntimeStatus(
        database=RuntimeDatabaseStatus(
            available=database_available,
            engine_label="PostgreSQL",
            placement_label="Remote PostgreSQL",
            host_label="db.internal",
        ),
    )


def test_app_health_snapshot_tracks_degraded_signals() -> None:
    settings = Settings(
        app_health_db_latency_warning_ms=100,
        app_health_db_latency_critical_ms=500,
        app_health_disk_warning_free_mb=1000,
        app_health_disk_critical_free_mb=250,
    )
    mebibyte = 1024 * 1024

    snapshot = build_app_health_snapshot(
        settings=settings,
        runtime_status=_runtime_status(),
        database_latency_ms=150,
        disk_usages=[
            SimpleNamespace(
                primary_path="/data",
                used_bytes=1300 * mebibyte,
                total_bytes=2000 * mebibyte,
            )
        ],
        active_lockout_count=2,
        cloudflare_block_count=1,
    )

    issue_keys = {issue.key for issue in snapshot.issues}
    assert snapshot.status == "degraded"
    assert snapshot.severity == "warning"
    assert "database.latency_warning" in issue_keys
    assert "disk./data" in issue_keys
    assert "security.login_lockout" in issue_keys
    assert "security.cloudflare_blocks" in issue_keys


def test_app_health_disk_alarm_uses_free_space_instead_of_percentage() -> None:
    mebibyte = 1024 * 1024
    settings = Settings(
        app_health_disk_warning_free_mb=1000,
        app_health_disk_critical_free_mb=250,
    )

    snapshot = build_app_health_snapshot(
        settings=settings,
        runtime_status=_runtime_status(),
        database_latency_ms=1,
        disk_usages=[
            SimpleNamespace(
                primary_path="/large-mostly-full",
                used_bytes=198_000 * mebibyte,
                total_bytes=200_000 * mebibyte,
            ),
            SimpleNamespace(
                primary_path="/small-low-free",
                used_bytes=301 * mebibyte,
                total_bytes=1200 * mebibyte,
            ),
        ],
        active_lockout_count=0,
        cloudflare_block_count=0,
    )

    issues_by_key = {issue.key: issue for issue in snapshot.issues}
    assert "disk./large-mostly-full" not in issues_by_key
    assert issues_by_key["disk./small-low-free"].severity == "warning"
    assert "899.0 MiB free" in issues_by_key["disk./small-low-free"].detail


def test_app_health_disk_alarm_is_critical_below_critical_free_space() -> None:
    mebibyte = 1024 * 1024
    snapshot = build_app_health_snapshot(
        settings=Settings(),
        runtime_status=_runtime_status(),
        database_latency_ms=1,
        disk_usages=[
            SimpleNamespace(
                primary_path="/data/backups",
                used_bytes=100 * mebibyte,
                total_bytes=10_000 * mebibyte,
                free_bytes=249 * mebibyte,
            )
        ],
        active_lockout_count=0,
        cloudflare_block_count=0,
    )

    disk_issue = next(issue for issue in snapshot.issues if issue.key == "disk./data/backups")
    assert disk_issue.severity == "critical"
    assert "249.0 MiB free" in disk_issue.detail


def test_app_health_snapshot_marks_database_outage_unavailable() -> None:
    snapshot = build_app_health_snapshot(
        settings=Settings(),
        runtime_status=_runtime_status(database_available=False),
        database_latency_ms=None,
        active_lockout_count=0,
        cloudflare_block_count=0,
    )

    assert snapshot.status == "unavailable"
    assert snapshot.severity == "critical"
    assert snapshot.banner_class == "critical"
    assert snapshot.banner_title == "App Unavailable"
    assert "database.unavailable" in {issue.key for issue in snapshot.issues}


def test_pushover_notifier_sends_degraded_once_and_restored(monkeypatch, tmp_path) -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_token="app-token",
        pushover_user="user-key",
        app_health_state_path=str(tmp_path / "app-health-state.json"),
    )
    calls: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, int]:
            return {"status": 1}

    def fake_post(_url, *, data, timeout):
        calls.append({"data": data, "timeout": timeout})
        return Response()

    monkeypatch.setattr("trip_tracker.services.app_health.httpx.post", fake_post)
    degraded = AppHealthSnapshot(
        status="degraded",
        severity="warning",
        issues=(
            AppHealthIssue(
                key="database.latency_warning",
                severity="warning",
                title="Database latency elevated",
                detail="Database round trip is 150.0 ms.",
            ),
        ),
        checked_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    restored = AppHealthSnapshot(
        status="ok",
        severity="ok",
        issues=(),
        checked_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
    )

    notifier = PushoverAppHealthNotifier(settings)

    assert notifier.notify_if_needed(degraded) is True
    assert notifier.notify_if_needed(degraded) is False
    assert notifier.notify_if_needed(restored) is True

    assert [call["data"]["title"] for call in calls] == [
        "Trip Tracker degraded",
        "Trip Tracker restored",
    ]
    assert calls[0]["data"]["token"] == "app-token"
    assert calls[0]["data"]["user"] == "user-key"


def test_pushover_notifier_repeats_unresolved_degraded_state_each_hour(
    monkeypatch,
    tmp_path,
) -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_token="app-token",
        pushover_user="user-key",
        app_health_state_path=str(tmp_path / "app-health-state.json"),
        app_health_reminder_interval_seconds=3600,
    )
    titles: list[str] = []

    def fake_send(_settings, *, title, message, priority=None):
        titles.append(title)

    monkeypatch.setattr(
        "trip_tracker.services.app_health.send_pushover_message",
        fake_send,
    )

    def degraded_snapshot(checked_at: datetime) -> AppHealthSnapshot:
        return AppHealthSnapshot(
            status="degraded",
            severity="warning",
            issues=(
                AppHealthIssue(
                    key="disk./data",
                    severity="warning",
                    title="Disk space low",
                    detail="/data has 900.0 MiB free.",
                ),
            ),
            checked_at=checked_at,
        )

    notifier = PushoverAppHealthNotifier(settings)
    first_alert_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    assert notifier.notify_if_needed(degraded_snapshot(first_alert_at)) is True
    notifier = PushoverAppHealthNotifier(settings)
    assert notifier.notify_if_needed(
        degraded_snapshot(first_alert_at.replace(minute=59))
    ) is False
    notifier = PushoverAppHealthNotifier(settings)
    assert notifier.notify_if_needed(
        degraded_snapshot(first_alert_at.replace(hour=13))
    ) is True
    notifier = PushoverAppHealthNotifier(settings)
    assert notifier.notify_if_needed(
        degraded_snapshot(first_alert_at.replace(hour=14))
    ) is True

    assert titles == [
        "Trip Tracker degraded",
        "Trip Tracker degraded reminder",
        "Trip Tracker degraded reminder",
    ]


def test_pushover_notifier_repeats_unresolved_unavailable_state_each_hour(
    monkeypatch,
    tmp_path,
) -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_token="app-token",
        pushover_user="user-key",
        app_health_state_path=str(tmp_path / "app-health-state.json"),
        app_health_reminder_interval_seconds=3600,
    )
    titles: list[str] = []
    monkeypatch.setattr(
        "trip_tracker.services.app_health.send_pushover_message",
        lambda _settings, *, title, message, priority=None: titles.append(title),
    )
    issue = AppHealthIssue(
        key="database.unavailable",
        severity="critical",
        title="PostgreSQL unavailable",
        detail="The configured database is not accepting app queries.",
    )
    notifier = PushoverAppHealthNotifier(settings)

    assert notifier.notify_if_needed(
        AppHealthSnapshot(
            status="unavailable",
            severity="critical",
            issues=(issue,),
            checked_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        )
    )
    assert notifier.notify_if_needed(
        AppHealthSnapshot(
            status="unavailable",
            severity="critical",
            issues=(issue,),
            checked_at=datetime(2026, 7, 27, 13, 0, tzinfo=UTC),
        )
    )

    assert titles == [
        "Trip Tracker unavailable",
        "Trip Tracker unavailable reminder",
    ]


def test_pushover_accepts_app_and_user_key_aliases() -> None:
    settings = Settings(
        pushover_enabled=True,
        pushover_app_key="alias-app-token",
        pushover_user_key="alias-user-key",
    )

    assert pushover_configured(settings)


def test_app_health_monitor_requires_sustained_latency_before_notification(
    monkeypatch,
) -> None:
    settings = Settings(
        app_health_monitor_interval_seconds=60,
        app_health_db_latency_sustained_seconds=15,
    )
    monitor = AppHealthMonitor(settings)
    now = [100.0]
    latency_snapshot = AppHealthSnapshot(
        status="degraded",
        severity="warning",
        issues=(
            AppHealthIssue(
                key="database.latency_warning",
                severity="warning",
                title="Database latency elevated",
                detail="Database round trip is 750.0 ms.",
            ),
        ),
        checked_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "trip_tracker.services.app_health.time.monotonic",
        lambda: now[0],
    )

    assert monitor._notification_snapshot(latency_snapshot) is None
    assert monitor._next_check_delay() == 15

    now[0] = 114.0
    assert monitor._notification_snapshot(latency_snapshot) is None
    assert monitor._next_check_delay() == 1

    now[0] = 115.0
    assert monitor._notification_snapshot(latency_snapshot) == latency_snapshot
    assert monitor._next_check_delay() == 60


def test_app_health_monitor_resets_latency_timer_after_recovery(monkeypatch) -> None:
    monitor = AppHealthMonitor(Settings(app_health_db_latency_sustained_seconds=15))
    now = [100.0]
    latency_snapshot = AppHealthSnapshot(
        status="degraded",
        severity="critical",
        issues=(
            AppHealthIssue(
                key="database.latency_critical",
                severity="critical",
                title="Database latency critical",
                detail="Database round trip is 2500.0 ms.",
            ),
        ),
        checked_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
    )
    healthy_snapshot = AppHealthSnapshot(
        status="ok",
        severity="ok",
        issues=(),
        checked_at=datetime(2026, 7, 13, 12, 0, 5, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "trip_tracker.services.app_health.time.monotonic",
        lambda: now[0],
    )

    assert monitor._notification_snapshot(latency_snapshot) is None
    now[0] = 105.0
    assert monitor._notification_snapshot(healthy_snapshot) == healthy_snapshot
    now[0] = 120.0
    assert monitor._notification_snapshot(latency_snapshot) is None
