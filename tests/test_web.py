import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from trip_tracker import __version__
from trip_tracker.app import app
from trip_tracker.config import Settings
from trip_tracker.database import get_db
from trip_tracker.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    Base,
    CloudflareIPBlock,
    DeletedTrip,
    GasPriceSnapshot,
    HiddenLoginFailure,
    MonthlyGasPrice,
    MonthlyReportExpense,
    OwnTracksLocation,
    OwnTracksMonthlySummary,
    PasskeyCredential,
    Site,
    Trip,
    TripProcessingCheckpoint,
    WebLoginAudit,
)
from trip_tracker.services.app_health import AppHealthIssue, AppHealthSnapshot
from trip_tracker.services.backups import create_automatic_backup, list_automatic_backup_files
from trip_tracker.services.cloudflare_blocks import (
    CloudflareAccessRule,
    CloudflareBlockError,
    create_cloudflare_ip_block,
    ip_is_allowlisted,
)
from trip_tracker.services.diagnostics import (
    paginated_owntracks_entries,
    recent_owntracks_entries,
)
from trip_tracker.services.gas_prices import AaaMichiganGasPriceProvider, GasPriceReading
from trip_tracker.services.login_failures import (
    tail_login_failure_entries,
    tail_login_success_entries,
)
from trip_tracker.services.mileage import (
    MANUAL_TRIP_NOTE,
    WAYPOINT_TRIP_NOTE,
    haversine_miles,
)
from trip_tracker.services.runtime_status import (
    RuntimeDatabaseStatus,
    RuntimeStatus,
    build_runtime_status,
)
from trip_tracker.web.auth import FAILED_LOGIN_ATTEMPTS
from trip_tracker.web.routes import (
    _current_year_month,
    _dashboard_distance_summary,
    _dashboard_reimbursement_summary,
    _database_latency_indicator_class,
    _diagnostic_database_summary,
    _diagnostic_disk_usages,
    _diagnostic_gas_price_extremes,
    _format_comma_number,
    _human_duration_since,
    templates,
)

TEST_SECRET_KEY = "test-secret-key-for-web-session-signing"


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


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _store_login_audit(
    db: Session,
    payload: dict,
    *,
    entry_id: str = "a" * 64,
) -> WebLoginAudit:
    """Store a structured login audit fixture in the database."""

    occurred_at = datetime.fromisoformat(payload["occurred_at_utc"].replace("Z", "+00:00"))
    audit = WebLoginAudit(
        entry_id=entry_id,
        event=payload["event"],
        occurred_at=occurred_at,
        payload=payload,
    )
    db.add(audit)
    db.commit()
    return audit


def _test_client_session(
    *,
    client_host: str = "testclient",
) -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, client=(client_host, 50000)), session_factory


def test_database_outage_renders_limp_mode_page(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://")

    def offline_get_db():
        raise OperationalError("SELECT 1", {}, Exception("database offline"))
        yield

    runtime_status = _runtime_status(database_available=False)
    monkeypatch.setattr("trip_tracker.app.settings", settings)
    monkeypatch.setattr(
        "trip_tracker.app.build_runtime_status",
        lambda _settings, *, database_available: runtime_status,
    )
    app.dependency_overrides[get_db] = offline_get_db
    try:
        response = TestClient(app).get("/diagnostics")

        assert response.status_code == 200
        assert response.headers["X-Trip-Tracker-Limp-Mode"] == "true"
        assert "Limp Mode" not in response.text
        assert "Database Unreachable" not in response.text
        assert "Service Temporarily Unavailable" in response.text
        assert "The application is currently offline." in response.text
        assert "This page will keep trying the app home page" in response.text
        assert 'window.location.replace("/")' in response.text
        assert "window.location.reload()" not in response.text
        assert "}, 30000);" in response.text
        assert '<header class="topbar">' not in response.text
        assert '<div class="brand"' not in response.text
        assert '<nav aria-label="Primary navigation">' not in response.text
        assert "/static/icons/trip-tracker-icon.svg" not in response.text
        assert 'rel="manifest" href="/manifest.webmanifest"' not in response.text
        assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' not in response.text
        assert 'navigator.serviceWorker.register("/service-worker.js"' not in response.text
        assert "<title>Service Temporarily Unavailable</title>" in response.text
        assert "PostgreSQL Server" not in response.text
        assert "<dt>Database</dt>" not in response.text
        assert "Remote PostgreSQL - db.internal" not in response.text
        assert "db.internal" not in response.text
        assert "192.168.1.20" not in response.text
        assert "connect to" not in response.text
        assert "OwnTracks Intake" not in response.text
        assert "Accepting and buffering" not in response.text
        assert "Queued OwnTracks Payloads" not in response.text
        assert "Queued Payloads" not in response.text
        assert "Primary Buffer" not in response.text
        assert "Backup Buffer" not in response.text
        assert "Primary Queue" not in response.text
        assert "Backup Queue" not in response.text
        assert "Last Replay Error" not in response.text
        assert "Buffered OwnTracks data" not in response.text
        assert "Oldest Queued Payload" not in response.text
        assert "Oldest Received Payload" not in response.text
        assert "2 hours ago" not in response.text
        assert 'class="limp-mode-status"' not in response.text
        assert 'class="limp-mode-buffer-card"' not in response.text
        assert "font-size: clamp(16px, 5vw, 36px);" in response.text
    finally:
        app.dependency_overrides.clear()


def test_database_outage_content_fetch_renders_limp_mode_fragment(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://")

    def offline_get_db():
        raise OperationalError("SELECT 1", {}, Exception("database offline"))
        yield

    runtime_status = _runtime_status(database_available=False)
    monkeypatch.setattr("trip_tracker.app.settings", settings)
    monkeypatch.setattr(
        "trip_tracker.app.build_runtime_status",
        lambda _settings, *, database_available: runtime_status,
    )
    app.dependency_overrides[get_db] = offline_get_db
    try:
        response = TestClient(app).get(
            "/dashboard/content",
            headers={"X-Requested-With": "fetch"},
        )

        assert response.status_code == 200
        assert response.headers["X-Trip-Tracker-Limp-Mode"] == "true"
        assert "Service Temporarily Unavailable" in response.text
        assert "The application is currently offline." in response.text
        assert "Database Unreachable" not in response.text
        assert "Limp Mode" not in response.text
        assert "<dt>Database</dt>" not in response.text
        assert "Queued OwnTracks Payloads" not in response.text
        assert "Queued Payloads" not in response.text
        assert "Primary Queue" not in response.text
        assert "Backup Queue" not in response.text
        assert "Oldest Queued Payload" not in response.text
        assert "Last Replay Error" not in response.text
        assert "Buffered OwnTracks data" not in response.text
        assert 'window.location.replace("/login")' not in response.text
        assert 'window.location.replace("/")' not in response.text
        assert "window.location.reload()" not in response.text
        assert '<header class="topbar">' not in response.text
        assert "<!doctype html>" not in response.text
        assert response.text.count('class="limp-mode-shell"') == 1
    finally:
        app.dependency_overrides.clear()


def test_database_outage_limp_mode_hides_top_navigation(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    runtime_status = _runtime_status(database_available=False)
    monkeypatch.setattr("trip_tracker.app.settings", settings)
    monkeypatch.setattr(
        "trip_tracker.app.build_runtime_status",
        lambda _settings, *, database_available: runtime_status,
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)

    def offline_get_db():
        raise OperationalError("SELECT 1", {}, Exception("database offline"))
        yield

    client, _ = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/",
            },
            follow_redirects=False,
        )
        app.dependency_overrides[get_db] = offline_get_db
        response = client.get("/")

        assert login_response.status_code == 303
        assert response.status_code == 200
        assert response.headers["X-Trip-Tracker-Limp-Mode"] == "true"
        assert '<header class="topbar">' not in response.text
        assert '<div class="brand"' not in response.text
        assert '<nav aria-label="Primary navigation">' not in response.text
        assert '<a class="nav-link nav-link-home"' not in response.text
        assert 'href="/trips"' not in response.text
        assert 'href="/waypoints"' not in response.text
        assert 'href="/diagnostics"' not in response.text
        assert 'action="/logout"' not in response.text
        assert 'aria-disabled="true"' not in response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_runtime_status_classifies_postgresql_remote_and_local() -> None:
    remote_status = build_runtime_status(
        Settings(
            database_url="postgresql+psycopg://triptracker:secret@db.internal:5432/trip_tracker",
        ),
        database_available=True,
    )
    local_status = build_runtime_status(
        Settings(
            database_url="postgresql+psycopg://triptracker:secret@postgres:5432/trip_tracker",
        ),
        database_available=False,
    )

    assert remote_status.database.state_label == "Reachable"
    assert remote_status.database.detail_label == "Remote PostgreSQL - db.internal"
    assert local_status.database.state_label == "Unavailable"
    assert local_status.database.detail_label == "Local/Bundled PostgreSQL - postgres"


def _html_section(html: str, start_marker: str, end_marker: str | None = None) -> str:
    start = html.index(start_marker)
    if end_marker is None:
        return html[start:]
    end = html.index(end_marker, start)
    return html[start:end]


def test_comma_number_filter_formats_large_display_values() -> None:
    assert _format_comma_number(999) == "999"
    assert _format_comma_number(1_000) == "1,000"
    assert _format_comma_number(10_000) == "10,000"
    assert _format_comma_number(100_000) == "100,000"
    assert _format_comma_number(1_000_000) == "1,000,000"
    assert _format_comma_number(Decimal("12345.678"), 1) == "12,345.7"
    assert _format_comma_number(Decimal("12345.678"), 2) == "12,345.68"


def _location(
    captured_at: datetime,
    received_at: datetime,
    raw_payload: dict,
    latitude: str = "42.3314000",
    longitude: str = "-83.0458000",
    odometer_miles: Decimal | None = None,
) -> OwnTracksLocation:
    return OwnTracksLocation(
        captured_at=captured_at,
        received_at=received_at,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        odometer_miles=odometer_miles,
        odometer_source="owntracks_rolling" if odometer_miles is not None else None,
        raw_payload=raw_payload,
    )


def _site(
    name: str,
    latitude: str = "42.3314000",
    longitude: str = "-83.0458000",
    *,
    active: bool = True,
) -> Site:
    return Site(
        name=name,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        radius_m=150,
        active=active,
    )


def _seed_full_backup_data(db: Session) -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    site = Site(
        name="Client",
        latitude=Decimal("42.3314000"),
        longitude=Decimal("-83.0458000"),
        radius_m=175,
        active=True,
        created_at=now,
        last_visited_at=now,
    )
    db.add(site)
    db.flush()

    location = _location(
        now,
        now + timedelta(seconds=5),
        {"_type": "transition", "event": "enter", "desc": "Client"},
        odometer_miles=Decimal("1000.0"),
    )
    db.add(location)
    db.flush()

    db.add_all(
        [
            Trip(
                trip_date=now.date(),
                origin_site_id=site.id,
                destination_site_id=site.id,
                started_at=now,
                ended_at=now + timedelta(minutes=20),
                start_latitude=site.latitude,
                start_longitude=site.longitude,
                end_latitude=site.latitude,
                end_longitude=site.longitude,
                origin_name="Client",
                destination_name="Client",
                miles=Decimal("12.3"),
                start_odometer_miles=Decimal("1000.0"),
                end_odometer_miles=Decimal("1012.3"),
                start_odometer_source="manual",
                end_odometer_source="estimated",
                mileage_source="owntracks_path",
                source="auto",
                notes="backup test trip",
                created_at=now,
                updated_at=now,
            ),
            DeletedTrip(
                deleted_trip_id=99,
                trip_date=now.date(),
                origin_site_id=site.id,
                destination_site_id=site.id,
                started_at=now + timedelta(hours=1),
                ended_at=now + timedelta(hours=2),
                origin_name="Client",
                destination_name="Client",
                miles=Decimal("0.4"),
                source="auto",
                mileage_source="owntracks_path",
                reason="invalid_same_waypoint_under_one_mile",
                deleted_at=now,
                notes="suppressed test trip",
            ),
            TripProcessingCheckpoint(
                name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
                last_owntracks_location_id=location.id,
                odometer_anchor_miles=Decimal("1012.3"),
                odometer_anchor_recorded_at=now,
                created_at=now,
                updated_at=now,
            ),
            GasPriceSnapshot(
                observed_on=now.date(),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("3.250"),
                source="test",
                source_detail="backup test",
                created_at=now,
            ),
            MonthlyGasPrice(
                year=2026,
                month=6,
                state="MI",
                average_price_per_gallon=Decimal("3.250"),
                buffer_per_gallon=Decimal("0.50"),
                effective_rate=Decimal("3.750"),
                source="test",
                source_detail="backup test",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.commit()


def test_recent_owntracks_entries_include_travel_entries() -> None:
    db = _session()
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(now, now, {"_type": "transition", "event": "enter", "desc": "Client A"}),
            _location(
                now + timedelta(minutes=1),
                now + timedelta(minutes=1),
                {"_type": "location"},
            ),
            _location(
                now + timedelta(minutes=2),
                now + timedelta(minutes=2),
                {"_type": "waypoint", "desc": "Client B"},
            ),
        ]
    )
    db.commit()

    events = recent_owntracks_entries(db)

    assert [event.raw_payload["_type"] for event in events] == [
        "transition",
        "location",
        "waypoint",
    ]


def test_paginated_owntracks_entries_loads_requested_page() -> None:
    db = _session()
    start = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)
    db.add_all(
        [
            _location(
                start + timedelta(minutes=index),
                start + timedelta(minutes=index),
                {"_type": "location", "index": index},
            )
            for index in range(45)
        ]
    )
    db.commit()

    page = paginated_owntracks_entries(db, page=2, page_size=20)

    assert page.total == 45
    assert page.page == 2
    assert page.total_pages == 3
    assert page.first_item == 21
    assert page.last_item == 40
    assert page.has_previous is True
    assert page.has_next is True
    assert len(page.entries) == 20
    assert page.entries[0].raw_payload["index"] == 5
    assert page.entries[-1].raw_payload["index"] == 24


def test_web_login_redirects_browser_pages_when_configured(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/trips?year=2026&month=6", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_leaves_api_routes_open(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_accepts_configured_credentials(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session(client_host="172.18.0.5")
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/trips?year=2026&month=6",
            },
            follow_redirects=False,
        )
        page_response = client.get("/trips/content?year=2026&month=6")

        assert login_response.status_code == 303
        assert login_response.headers["location"] == "/trips?year=2026&month=6"
        assert page_response.status_code == 200
        assert "Monthly Work Trips" in page_response.text
        with session_factory() as db:
            success_entries = tail_login_success_entries(db)
            stored_payload = db.scalar(select(WebLoginAudit)).payload
        assert len(success_entries) == 1
        assert success_entries[0].username == "admin"
        assert success_entries[0].account == "admin"
        assert success_entries[0].authentication_method == "password"
        assert success_entries[0].authentication_method_label == "Password"
        assert "secret-password" not in json.dumps(stored_payload)
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_rejects_invalid_credentials(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session(client_host="172.18.0.5")
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/trips?year=2026&month=6",
            },
        )
        page_response = client.get("/trips?year=2026&month=6", follow_redirects=False)

        assert login_response.status_code == 200
        assert '<p class="form-error login-status-line" role="alert">' in login_response.text
        assert "Invalid username or password." in login_response.text
        error_index = login_response.text.index("Invalid username or password.")
        username_index = login_response.text.index("Username")
        assert error_index < username_index
        assert page_response.status_code == 303
        with session_factory() as db:
            assert db.scalar(select(func.count(WebLoginAudit.id))) == 1
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_records_failed_attempt_audit_log(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session(client_host="172.18.0.5")
    try:
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/diagnostics",
            },
            headers={
                "User-Agent": "ExampleBrowser/1.0",
                "CF-Connecting-IP": "198.51.100.77",
                "X-Real-IP": "203.0.113.10",
                "X-Forwarded-For": "203.0.113.10, 10.0.0.8",
                "X-Forwarded-Proto": "https",
            },
        )

        with session_factory() as db:
            payload = db.scalar(select(WebLoginAudit)).payload

        assert response.status_code == 200
        assert payload["event"] == "web_login_failed"
        assert payload["client_ip"] == "198.51.100.77"
        assert payload["cf_connecting_ip"] == "198.51.100.77"
        assert payload["username"] == "admin"
        assert payload["password_length"] == len("wrong-password")
        assert payload["user_agent"] == "ExampleBrowser/1.0"
        assert payload["reason"] == "invalid_credentials"
        assert payload["next_url"] == "/diagnostics"
        assert payload["failed_count"] == 1
        assert "wrong-password" not in json.dumps(payload)
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_uses_cloudflare_client_ip_when_header_is_present(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session(client_host="203.0.113.88")
    try:
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/diagnostics",
            },
            headers={
                "CF-Connecting-IP": "198.51.100.77",
                "X-Real-IP": "198.51.100.78",
                "X-Forwarded-For": "198.51.100.79, 172.18.0.5",
            },
        )

        with session_factory() as db:
            payload = db.scalar(select(WebLoginAudit)).payload

        assert response.status_code == 200
        assert payload["client_ip"] == "198.51.100.77"
        assert payload["direct_client_ip"] == "203.0.113.88"
        assert payload["cf_connecting_ip"] == "198.51.100.77"
        assert list(FAILED_LOGIN_ATTEMPTS) == ["198.51.100.77"]
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_failed_login_entries_use_stored_effective_client_ip() -> None:
    stale_payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-27T12:00:00Z",
        "occurred_at_local": "2026-06-27T08:00:00-04:00",
        "client_ip": "198.51.100.77",
        "direct_client_ip": "172.18.0.5",
        "cf_connecting_ip": "198.51.100.77",
        "x_real_ip": "127.0.0.1",
        "x_forwarded_for": "203.0.113.44, 172.18.0.5",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    settings = Settings(database_url="sqlite://")
    db = _session()
    _store_login_audit(db, stale_payload)

    entries = tail_login_failure_entries(db, settings=settings)

    assert len(entries) == 1
    assert entries[0].client_ip == "198.51.100.77"
    assert entries[0].direct_client_ip == "172.18.0.5"


def test_successful_login_entries_use_stored_effective_client_ip_for_diagnostics() -> None:
    payload = {
        "event": "web_login_succeeded",
        "occurred_at_utc": "2026-06-27T12:00:00Z",
        "occurred_at_local": "2026-06-27T08:00:00-04:00",
        "client_ip": "172.18.0.5",
        "direct_client_ip": "172.18.0.5",
        "cf_connecting_ip": "198.51.100.77",
        "x_real_ip": "198.51.100.77",
        "x_forwarded_for": "198.51.100.77",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "account": "admin",
    }
    db = _session()
    _store_login_audit(db, payload)

    entries = tail_login_success_entries(db)

    assert len(entries) == 1
    assert entries[0].client_ip == "172.18.0.5"
    assert entries[0].authentication_method == "password"

    diagnostics_entries = tail_login_success_entries(db, settings=Settings())

    assert len(diagnostics_entries) == 1
    assert diagnostics_entries[0].client_ip == "172.18.0.5"


def test_failed_login_entries_ignore_forwarding_headers_after_audit_write() -> None:
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-27T12:00:00Z",
        "occurred_at_local": "2026-06-27T08:00:00-04:00",
        "client_ip": "203.0.113.88",
        "direct_client_ip": "203.0.113.88",
        "cf_connecting_ip": "198.51.100.77",
        "x_real_ip": "198.51.100.78",
        "x_forwarded_for": "198.51.100.79",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    settings = Settings(database_url="sqlite://")
    db = _session()
    _store_login_audit(db, payload)

    entries = tail_login_failure_entries(db, settings=settings)

    assert len(entries) == 1
    assert entries[0].client_ip == "203.0.113.88"


def test_web_login_page_does_not_disclose_app_name(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/login")

        assert response.status_code == 200
        assert "Trip Tracker" not in response.text
        assert "Mileage" not in response.text
        assert ">ML<" not in response.text
        assert 'name="application-name"' not in response.text
        assert 'name="apple-mobile-web-app-title"' not in response.text
        assert 'rel="manifest" href="/manifest.webmanifest"' not in response.text
        assert 'rel="icon"' not in response.text
        assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' not in response.text
        assert "/static/icons/trip-tracker" not in response.text
        assert "<title>Sign In</title>" in response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_public_device_checkbox_disables_and_restores_device_sign_in(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                PasskeyCredential(
                    credential_id="public-device-passkey",
                    user_handle="public-device-user",
                    username="admin",
                    public_key="public-key",
                    transports=[],
                )
            )
            db.commit()

        response = client.get("/login")

        assert response.status_code == 200
        assert 'id="public-device-checkbox"' in response.text
        assert "This is a public device" in response.text
        assert 'aria-describedby="public-device-tooltip"' in response.text
        assert 'class="public-device-tooltip" role="tooltip"' in response.text
        assert "15 minutes of inactivity" in response.text
        assert ".public-device-option:hover .public-device-tooltip" in response.text
        assert ".public-device-option:focus-within .public-device-tooltip" in response.text
        assert 'id="passkey-login-button"' in response.text
        assert 'publicDeviceCheckbox.addEventListener("change"' in response.text
        assert "button.disabled = publicDevice" in response.text
        assert "Device Sign-In is unavailable on a public device." in response.text
    finally:
        app.dependency_overrides.clear()


def test_public_device_session_expires_and_clears_browser_data(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    current_time = [1_000.0]
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.auth.time.time", lambda: current_time[0])
    client, _ = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "public_device": "true",
            },
            follow_redirects=False,
        )
        active_response = client.get("https://testserver/", follow_redirects=False)
        current_time[0] += 901
        expired_response = client.get(
            "https://testserver/diagnostics",
            follow_redirects=False,
        )

        assert login_response.status_code == 303
        assert active_response.status_code == 200
        assert "idleTimeoutMs = 900 * 1000" in active_response.text
        assert 'href="/manifest.webmanifest"' not in active_response.text
        assert expired_response.status_code == 303
        assert expired_response.headers["location"] == "/login?public_timeout=1"
        assert expired_response.headers["clear-site-data"] == '"cache", "cookies", "storage"'
        assert "trip_tracker_session=null" in expired_response.headers["set-cookie"]
    finally:
        app.dependency_overrides.clear()


def test_passkey_login_rejects_public_device_mode(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.post(
            "/passkeys/login/options",
            json={"next_url": "/", "public_device": True},
        )

        assert response.status_code == 400
        assert response.json() == {
            "error": "Device Sign-In is unavailable on a public device."
        }
    finally:
        app.dependency_overrides.clear()


def test_login_page_shows_device_sign_in_when_passkey_exists(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                PasskeyCredential(
                    credential_id="YWJj",
                    user_handle="user-handle",
                    username="admin",
                    public_key="public-key",
                    transports=[],
                )
            )
            db.commit()

        response = client.get("/login")

        assert response.status_code == 200
        assert "Device Sign-In" in response.text
        assert "/passkeys/login/options" in response.text
        assert response.text.index("Continue") < response.text.index("Device Sign-In")
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_passkey_login_options_stay_open_and_use_browser_origin(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                PasskeyCredential(
                    credential_id="YWJj",
                    user_handle="user-handle",
                    username="admin",
                    public_key="public-key",
                    transports=[],
                )
            )
            db.commit()

        response = client.post(
            "/passkeys/login/options",
            json={"next_url": "/diagnostics"},
            headers={"Origin": "https://mileage.example"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["rpId"] == "mileage.example"
        assert payload["allowCredentials"][0]["id"] == "YWJj"
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_passkey_login_verify_authenticates_session(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            passkey = PasskeyCredential(
                credential_id="YWJj",
                user_handle="user-handle",
                username="admin",
                public_key="public-key",
                transports=[],
            )
            db.add(passkey)
            db.commit()
            passkey_id = passkey.id

        def verify_passkey(db: Session, _request, _payload):
            return db.get(PasskeyCredential, passkey_id)

        monkeypatch.setattr(
            "trip_tracker.web.routes.finish_passkey_authentication",
            verify_passkey,
        )
        verify_response = client.post(
            "/passkeys/login/verify",
            json={"id": "credential", "next_url": "/diagnostics"},
        )
        diagnostics_response = client.get("/diagnostics")

        assert verify_response.status_code == 200
        assert verify_response.json() == {"redirect_url": "/diagnostics"}
        assert diagnostics_response.status_code == 200
        with session_factory() as db:
            success_entries = tail_login_success_entries(db)
        assert len(success_entries) == 1
        assert success_entries[0].username == "admin"
        assert success_entries[0].authentication_method == "passkey"
        success_section = _html_section(
            diagnostics_response.text,
            '<section id="login-successes" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Passkey" in success_section
        assert "Password" not in success_section
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_failed_passkey_login_records_audit_entry(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        def reject_passkey(_db, _request, _payload):
            from trip_tracker.services.passkeys import PasskeyCeremonyError

            raise PasskeyCeremonyError("invalid")

        monkeypatch.setattr(
            "trip_tracker.web.routes.finish_passkey_authentication",
            reject_passkey,
        )
        response = client.post(
            "/passkeys/login/verify",
            json={"id": "credential", "next_url": "/diagnostics"},
        )

        assert response.status_code == 401
        with session_factory() as db:
            payload = db.scalar(select(WebLoginAudit)).payload
        assert payload["event"] == "web_login_failed"
        assert payload["reason"] == "invalid_passkey"
        assert payload["password_length"] == 0
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_layout_includes_mobile_install_metadata(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, _ = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/",
            },
            follow_redirects=False,
        )
        response = client.get("/")

        assert login_response.status_code == 303
        assert response.status_code == 200
        assert (
            "Loading mileage totals, reimbursement details, and recent work trips."
            in response.text
        )
        assert 'data-dashboard-content-url="/dashboard/content"' in response.text
        assert 'response.headers.get("X-Trip-Tracker-Limp-Mode") === "true"' in response.text
        assert 'window.location.replace("/")' in response.text
        assert (
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            in response.text
        )
        assert "viewport-fit=cover" not in response.text
        assert 'name="apple-mobile-web-app-capable" content="yes"' in response.text
        assert (
            'name="apple-mobile-web-app-status-bar-style" content="black-translucent"'
            in response.text
        )
        assert 'rel="manifest" href="/manifest.webmanifest"' in response.text
        assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' in response.text
        assert "/static/icons/trip-tracker-icon.svg" in response.text
        assert "/static/icons/trip-tracker-brand.png" in response.text
        assert '<div class="brand" aria-label="Trip Tracker">' in response.text
        assert '<span class="brand-title">Trip Tracker</span>' in response.text
        assert f'<span class="brand-version">v{__version__}</span>' in response.text
        assert '<a class="brand" href="/">' not in response.text
        assert '<nav aria-label="Primary navigation">' in response.text
        assert (
            '<a class="nav-link nav-link-home" href="/" aria-label="Home" title="Home">'
            in response.text
        )
        assert (
            '<a class="nav-link nav-link-trips" href="/trips" aria-label="Work Trips" '
            'title="Work Trips">'
            in response.text
        )
        assert '<span class="nav-label">Work Trips</span>' in response.text
        assert '<span class="nav-label">Diagnostics</span>' in response.text
        assert (
            '<button type="submit" class="nav-logout" aria-label="Logout" title="Logout">'
            in response.text
        )
        assert '<span class="nav-label">Logout</span>' in response.text
        assert response.text.count('class="nav-icon"') == 5
        assert "--nav-mobile-bg: linear-gradient(180deg, #3b82f6, #1d4ed8);" in response.text
        assert "--nav-mobile-bg: rgba(239, 111, 108, 0.2);" not in response.text
        assert "transform: translateY(3px);" in response.text
        assert ".button-link:hover" in response.text
        assert ".nav-icon {\n  display: block;" in response.text
        assert "gap: 7px;" in response.text
        assert "background: var(--nav-mobile-bg);" in response.text
        assert "border: 1px solid var(--nav-mobile-border);" in response.text
        assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in response.text
        assert ".topbar-actions {\n  grid-column: 2;" in response.text
        assert "justify-content: center;" in response.text
        assert "justify-content: space-between;" in response.text
        assert "flex: 0 1 clamp(46px, 14vw, 58px);" in response.text
        assert "font-size: 0;" in response.text
        assert "clip-path: inset(50%);" in response.text
        assert 'class="app-close-button"' not in response.text
        assert "window.close()" not in response.text
        assert "border: 1px solid var(--line);" in response.text
        assert ".brand {\n    display: none;" in response.text
        assert "justify-content: stretch;" in response.text
        assert "position: fixed;\n    right: 0;\n    bottom: 0;" not in response.text
        assert "calc(20px + env(safe-area-inset-bottom))" in response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_login_page_has_no_top_navigation_for_unauthenticated_session(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/login")

        assert response.status_code == 200
        assert '<header class="topbar">' not in response.text
        assert '<nav aria-label="Primary navigation">' not in response.text
        assert '<a class="nav-link nav-link-home"' not in response.text
        assert '<button type="submit" class="nav-logout"' not in response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_trips_page_renders_loading_shell() -> None:
    client, _ = _test_client_session()
    try:
        response = client.get("/trips?year=2026&month=6")

        assert response.status_code == 200
        assert "Loading selected-month cards and work trip records." in response.text
        assert 'data-trips-content-url="/trips/content?year=2026&amp;month=6"' in response.text
        assert 'response.headers.get("X-Trip-Tracker-Limp-Mode") === "true"' in response.text
        assert 'window.location.replace("/")' in response.text
        assert "Monthly Work Trips" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_authenticated_pages_use_compact_page_intro_and_footer_layout() -> None:
    client, _ = _test_client_session()
    try:
        dashboard_response = client.get("/dashboard/content")
        trips_response = client.get("/trips/content?year=2026&month=6")
        waypoints_response = client.get("/waypoints")
        diagnostics_response = client.get("/diagnostics")

        assert dashboard_response.status_code == 200
        assert '<section class="page-header">' not in dashboard_response.text
        assert "<h1>Dashboard</h1>" not in dashboard_response.text
        assert "Review automatic OwnTracks intake" not in dashboard_response.text
        assert '<footer class="page-footer">' in dashboard_response.text
        assert dashboard_response.text.index("<h2>Recent Work Trips</h2>") < (
            dashboard_response.text.index("App time:")
        )

        assert trips_response.status_code == 200
        assert '<section class="page-header trips-page-header">' in trips_response.text
        trip_actions_index = trips_response.text.index('<div class="header-actions">')
        trip_title_index = trips_response.text.index("<h1>Work Trips</h1>")
        trip_description_index = trips_response.text.index("Showing June 2026 (06/2026)")
        trip_summary_index = trips_response.text.index(
            '<section class="trips-summary-grid"'
        )
        assert trip_title_index < trip_description_index < trip_actions_index < trip_summary_index

        assert waypoints_response.status_code == 200
        assert '<section class="page-header">' not in waypoints_response.text
        assert "<h1>Waypoints</h1>" not in waypoints_response.text
        assert "OwnTracks waypoints saved by the app" not in waypoints_response.text
        assert waypoints_response.text.index("<h2>OwnTracks Waypoints</h2>") < (
            waypoints_response.text.index('href="/waypoints/export"')
        )

        assert diagnostics_response.status_code == 200
        assert '<section class="page-header">' not in diagnostics_response.text
        assert "<h1>Diagnostics</h1>" not in diagnostics_response.text
        assert "Runtime status, data counts, and operational diagnostics." not in (
            diagnostics_response.text
        )
    finally:
        app.dependency_overrides.clear()


def test_install_assets_stay_available_when_web_login_is_enabled(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    client, _ = _test_client_session()
    try:
        manifest_response = client.get("/manifest.webmanifest", follow_redirects=False)
        service_worker_response = client.get("/service-worker.js", follow_redirects=False)
        favicon_response = client.get("/favicon.ico", follow_redirects=False)
        apple_icon_response = client.get("/apple-touch-icon.png", follow_redirects=False)
        brand_icon_response = client.get(
            "/static/icons/trip-tracker-brand.png",
            follow_redirects=False,
        )
        svg_icon_response = client.get(
            "/static/icons/trip-tracker-icon.svg",
            follow_redirects=False,
        )

        assert manifest_response.status_code == 200
        assert manifest_response.headers["content-type"].startswith("application/manifest+json")
        manifest = manifest_response.json()
        assert manifest["display"] == "standalone"
        assert manifest["display_override"] == ["standalone", "minimal-ui", "browser"]
        assert manifest["start_url"] == "/"
        assert manifest["scope"] == "/"
        assert manifest_response.headers["cache-control"] == "no-store"
        assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
        assert "/static/icons/trip-tracker-icon-512.png?v=1.5.0-brand1" in {
            icon["src"] for icon in manifest["icons"]
        }

        assert service_worker_response.status_code == 200
        assert service_worker_response.headers["cache-control"] == "no-store"
        assert service_worker_response.headers["service-worker-allowed"] == "/"
        assert "fetch(event.request)" in service_worker_response.text
        assert "caches.open" not in service_worker_response.text

        assert favicon_response.status_code == 200
        assert favicon_response.headers["content-type"].startswith("image/x-icon")
        assert favicon_response.headers["cache-control"] == "no-store"
        assert apple_icon_response.status_code == 200
        assert apple_icon_response.headers["content-type"].startswith("image/png")
        assert apple_icon_response.headers["cache-control"] == "no-store"
        assert brand_icon_response.status_code == 200
        assert brand_icon_response.headers["content-type"].startswith("image/png")
        assert svg_icon_response.status_code == 200
        assert svg_icon_response.headers["content-type"].startswith("image/svg+xml")
        assert "data:image/png;base64," in svg_icon_response.text
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_dashboard_shows_today_and_month_distance_totals(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        10,
        0,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: dashboard_now.date())
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        month_origin_latitude = Decimal("42.3314")
        month_origin_longitude = Decimal("-83.0458")
        prior_month_latitude = Decimal("42.3314")
        prior_month_longitude = Decimal("-83.0458")
        prior_today_latitude = Decimal("42.3440")
        prior_today_longitude = Decimal("-83.0600")
        today_point_one_latitude = Decimal("42.3314")
        today_point_one_longitude = Decimal("-83.0600")
        today_point_two_latitude = Decimal("42.3380")
        today_point_two_longitude = Decimal("-83.0700")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                today_point_one_latitude,
                today_point_one_longitude,
            )
            + haversine_miles(
                today_point_one_latitude,
                today_point_one_longitude,
                today_point_two_latitude,
                today_point_two_longitude,
            )
        ).quantize(Decimal("0.1"))
        month_total = (
            haversine_miles(
                prior_month_latitude,
                prior_month_longitude,
                month_origin_latitude,
                month_origin_longitude,
            )
            + haversine_miles(
                month_origin_latitude,
                month_origin_longitude,
                prior_today_latitude,
                prior_today_longitude,
            )
            + today_total
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_month_latitude),
                        longitude=str(prior_month_longitude),
                        odometer_miles=Decimal("80.0"),
                    ),
                    _location(
                        datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(month_origin_latitude),
                        longitude=str(month_origin_longitude),
                        odometer_miles=Decimal("95.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(today_point_one_latitude),
                        longitude=str(today_point_one_longitude),
                        odometer_miles=Decimal("102.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(today_point_two_latitude),
                        longitude=str(today_point_two_longitude),
                        odometer_miles=Decimal("108.5"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 12),
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("4.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.5"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        assert "Distance driven summary" in response.text
        assert "Work Trips + Non-Work Trips" in response.text
        assert "Work Trips only" in response.text
        assert (
            "<span>Today</span>\n"
            f"      <strong>{max(today_total, Decimal('5.5'))}</strong>\n"
            "      <small>Work Trips + Non-Work Trips</small>"
        ) in response.text
        assert "<strong>5.5</strong>" in response.text
        assert (
            "<span>This Month</span>\n"
            f"      <strong>{max(month_total, Decimal('9.5'))}</strong>\n"
            "      <small>Work Trips + Non-Work Trips</small>"
        ) in response.text
        assert "<strong>9.5</strong>" in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_count_cards_reset_at_detroit_month_boundary(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        7,
        1,
        0,
        30,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: dashboard_now.date())
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 7, 1, 3, 59, tzinfo=UTC),
                        datetime(2026, 7, 1, 3, 59, tzinfo=UTC),
                        {"_type": "location"},
                    ),
                    _location(
                        datetime(2026, 7, 1, 4, 0, tzinfo=UTC),
                        datetime(2026, 7, 1, 4, 0, tzinfo=UTC),
                        {"_type": "location"},
                    ),
                    _location(
                        datetime(2026, 7, 1, 4, 30, tzinfo=UTC),
                        datetime(2026, 7, 1, 4, 30, tzinfo=UTC),
                        {"_type": "location"},
                    ),
                    Trip(
                        trip_date=date(2026, 6, 28),
                        started_at=datetime(2026, 6, 28, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 28, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("4.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 30),
                        started_at=datetime(2026, 7, 1, 3, 30, tzinfo=UTC),
                        ended_at=datetime(2026, 7, 1, 3, 50, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 7, 1),
                        started_at=datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 7, 1, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("6.0"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        stats_section = _html_section(
            response.text,
            '<section class="stats-grid">',
            '<section class="distance-grid"',
        )
        assert "2026-07-01 12:30:00 AM" in response.text
        assert (
            "<span>OwnTracks Events</span>\n"
            "    <strong>2</strong>"
        ) in stats_section
        assert (
            "<span>Work Trips</span>\n"
            "    <dl class=\"work-trip-counts\" aria-label=\"Work Trips counts\">"
        ) in stats_section
        assert "<dt>Today</dt>\n        <dd>1</dd>" in stats_section
        assert "<dt>Week</dt>\n        <dd>2</dd>" in stats_section
        assert "<dt>Month</dt>\n        <dd>1</dd>" in stats_section
        assert "This Week" not in stats_section
        assert "This Month" not in stats_section
    finally:
        app.dependency_overrides.clear()


def test_dashboard_top_cards_format_large_numbers_with_commas() -> None:
    rendered = templates.env.get_template("dashboard_content.html").render(
        {
            "year": 2026,
            "month": 6,
            "location_count": 12_345,
            "trip_count": 1_234,
            "work_trip_counts": {
                "today": 100_000,
                "week": 123_456,
                "month": 1_234,
            },
            "distance_summary": {
                "today_total": Decimal("10001.2"),
                "today_trips": Decimal("9999.9"),
                "today_non_trips": Decimal("1.3"),
                "month_total": Decimal("123456.7"),
                "month_trips": Decimal("23456.7"),
                "month_non_trips": Decimal("100000.0"),
            },
            "reimbursement_summary": {
                "total": Decimal("12345.67"),
                "mileage_total": Decimal("12345.67"),
                "expense_total": Decimal("0.00"),
                "total_miles": Decimal("98765.4"),
                "reimbursement_gallons": Decimal("1234.56"),
                "reimbursement_gallons_display": "1234.5",
            },
            "location_state": {
                "state": "travel",
                "label": "Driving",
                "detail": "Last movement 1.0 miles",
            },
            "latest_odometer": {
                "value": Decimal("12345.6"),
                "source": "owntracks_estimate",
                "position": "Rolling",
                "recorded_at": datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
            },
            "recent_trips": [],
            "monthly_gas": SimpleNamespace(average_price_per_gallon=Decimal("1234.567")),
            "app_local_datetime": datetime(
                2026,
                6,
                16,
                9,
                30,
                tzinfo=ZoneInfo("America/Detroit"),
            ),
            "app_timezone": "America/Detroit",
            "app_timezone_abbr": "EDT",
        }
    )

    stats_section = _html_section(
        rendered,
        '<section class="stats-grid">',
        '<section class="distance-grid"',
    )
    distance_section = _html_section(
        rendered,
        '<section class="distance-grid"',
        '<section class="panel">',
    )
    assert "<strong>12,345</strong>" in stats_section
    assert "<dd>100,000</dd>" in stats_section
    assert "<dd>123,456</dd>" in stats_section
    assert "<dd>1,234</dd>" in stats_section
    assert "$12,345.67" in stats_section
    assert "1,234.5 reimbursement gallons" in stats_section
    assert "$1,234.567" in stats_section
    assert "12,345.6" in stats_section
    assert "<strong>10,001.2</strong>" in distance_section
    assert "<strong>9,999.9</strong>" in distance_section
    assert "<strong>123,456.7</strong>" in distance_section
    assert "<strong>23,456.7</strong>" in distance_section


def test_dashboard_replaces_waypoints_card_with_month_reimbursement(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        10,
        0,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: dashboard_now.date())
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Client",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    Trip(
                        trip_date=date(2026, 6, 12),
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("25.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("77.4"),
                    ),
                    MonthlyGasPrice(
                        year=2026,
                        month=6,
                        state="MI",
                        average_price_per_gallon=Decimal("3.500"),
                        buffer_per_gallon=Decimal("0.00"),
                        effective_rate=Decimal("3.500"),
                        source="test",
                        source_detail="dashboard reimbursement test",
                    ),
                ]
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        stats_section = _html_section(
            response.text,
            '<section class="stats-grid">',
            '<section class="panel">',
        )
        assert "Waypoints" not in stats_section
        assert "Month Reimbursement" in stats_section
        assert "$14.34" in stats_section
        assert "4.0 reimbursement gallons" in stats_section
        assert "4.096 reimbursement gallons" not in stats_section
        assert "mi PDF total" not in stats_section
        assert stats_section.index("<span>Location State</span>") < stats_section.index(
            "<span>OwnTracks Events</span>"
        )
        assert stats_section.index("<span>OwnTracks Events</span>") < stats_section.index(
            "<span>Work Trips</span>"
        )
        assert stats_section.index("<span>Work Trips</span>") < stats_section.index(
            "<span>Month Reimbursement</span>"
        )
        with session_factory() as db:
            monthly_gas = db.scalar(select(MonthlyGasPrice))
            assert monthly_gas is not None
            reimbursement_summary = _dashboard_reimbursement_summary(
                db,
                year=2026,
                month=6,
                monthly_gas=monthly_gas,
                vehicle_mpg=Decimal("25.0"),
            )
            assert reimbursement_summary["total"] == Decimal("14.34")
            assert reimbursement_summary["reimbursement_gallons"] == Decimal("4.096")
            assert reimbursement_summary["reimbursement_gallons_display"] == "4.0"
    finally:
        app.dependency_overrides.clear()


def test_dashboard_reimbursement_summary_includes_extra_expenses() -> None:
    db = _session()
    db.add_all(
        [
            Trip(
                trip_date=date(2026, 6, 16),
                started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                start_latitude=Decimal("42.3314"),
                start_longitude=Decimal("-83.0458"),
                end_latitude=Decimal("42.3440"),
                end_longitude=Decimal("-83.0600"),
                miles=Decimal("25.0"),
            ),
            MonthlyGasPrice(
                year=2026,
                month=6,
                state="MI",
                average_price_per_gallon=Decimal("3.500"),
                buffer_per_gallon=Decimal("0.00"),
                effective_rate=Decimal("3.500"),
                source="test",
                source_detail="dashboard reimbursement test",
            ),
            MonthlyReportExpense(
                expense_date=date(2026, 6, 17),
                year=2026,
                month=6,
                reason="Parking",
                amount=Decimal("12.35"),
            ),
        ]
    )
    db.commit()

    summary = _dashboard_reimbursement_summary(
        db,
        year=2026,
        month=6,
        monthly_gas=db.scalar(select(MonthlyGasPrice)),
        vehicle_mpg=Decimal("25.0"),
    )

    assert summary["mileage_total"] == Decimal("3.50")
    assert summary["expense_total"] == Decimal("12.35")
    assert summary["total"] == Decimal("15.85")


def test_dashboard_trip_plus_non_trip_total_is_never_below_trip_total(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        10,
        0,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: dashboard_now.date())
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude="42.3314000",
                        longitude="-83.0458000",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 14, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 14, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("8.4"),
                    ),
                ]
            )
            db.commit()
            summary = _dashboard_distance_summary(
                db,
                today=date(2026, 6, 16),
                year=2026,
                month=6,
            )

        response = client.get("/dashboard/content")

        assert summary["today_total"] == Decimal("8.4")
        assert summary["today_trips"] == Decimal("8.4")
        assert summary["today_non_trips"] == Decimal("0.0")
        assert summary["today_total"] - summary["today_trips"] == summary["today_non_trips"]
        assert summary["month_total"] == Decimal("8.4")
        assert summary["month_total"] - summary["month_trips"] == summary["month_non_trips"]
        assert response.status_code == 200
        assert (
            "<span>Today</span>\n"
            "      <strong>8.4</strong>\n"
            "      <small>Work Trips + Non-Work Trips</small>"
        ) in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_keeps_today_distance_until_local_midnight(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        23,
        30,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    current_day = dashboard_now.date()
    next_day = current_day + timedelta(days=1)
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: current_day)
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        prior_today_latitude = Decimal("42.3314")
        prior_today_longitude = Decimal("-83.0458")
        point_one_latitude = Decimal("42.3314")
        point_one_longitude = Decimal("-83.0600")
        point_two_latitude = Decimal("42.3380")
        point_two_longitude = Decimal("-83.0700")
        next_day_latitude = Decimal("42.3500")
        next_day_longitude = Decimal("-83.0800")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                point_one_latitude,
                point_one_longitude,
            )
            + haversine_miles(
                point_one_latitude,
                point_one_longitude,
                point_two_latitude,
                point_two_longitude,
            )
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 55, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("100.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        datetime(2026, 6, 16, 4, 10, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(point_one_latitude),
                        longitude=str(point_one_longitude),
                        odometer_miles=Decimal("101.0"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 17, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(point_two_latitude),
                        longitude=str(point_two_longitude),
                        odometer_miles=Decimal("112.3"),
                    ),
                    _location(
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(next_day_latitude),
                        longitude=str(next_day_longitude),
                        odometer_miles=Decimal("125.0"),
                    ),
                    Trip(
                        trip_date=current_day,
                        started_at=datetime(2026, 6, 17, 1, 30, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 17, 2, 0, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("7.3"),
                    ),
                    Trip(
                        trip_date=next_day,
                        started_at=datetime(2026, 6, 17, 4, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 17, 4, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("12.7"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        assert "2026-06-16 11:30:00 PM" in response.text
        assert (
            "<span>Today</span>\n"
            f"      <strong>{max(today_total, Decimal('7.3'))}</strong>\n"
            "      <small>Work Trips + Non-Work Trips</small>"
        ) in response.text
        assert (
            "<span>Today</span>\n"
            "      <strong>7.3</strong>\n"
            "      <small>Work Trips only</small>"
        ) in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_distance_totals_ignore_manual_odometer_reset(monkeypatch) -> None:
    dashboard_now = datetime(
        2026,
        6,
        16,
        10,
        0,
        tzinfo=ZoneInfo("America/Detroit"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.local_now", lambda: dashboard_now)
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: dashboard_now.date())
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        prior_month_latitude = Decimal("42.3314")
        prior_month_longitude = Decimal("-83.0458")
        prior_today_latitude = Decimal("42.3440")
        prior_today_longitude = Decimal("-83.0600")
        reset_point_latitude = Decimal("42.3314")
        reset_point_longitude = Decimal("-83.0600")
        end_point_latitude = Decimal("42.3380")
        end_point_longitude = Decimal("-83.0700")
        today_total = (
            haversine_miles(
                prior_today_latitude,
                prior_today_longitude,
                reset_point_latitude,
                reset_point_longitude,
            )
            + haversine_miles(
                reset_point_latitude,
                reset_point_longitude,
                end_point_latitude,
                end_point_longitude,
            )
        ).quantize(Decimal("0.1"))
        month_total = (
            haversine_miles(
                prior_month_latitude,
                prior_month_longitude,
                prior_today_latitude,
                prior_today_longitude,
            )
            + today_total
        ).quantize(Decimal("0.1"))
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        datetime(2026, 6, 1, 3, 30, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_month_latitude),
                        longitude=str(prior_month_longitude),
                        odometer_miles=Decimal("5.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        datetime(2026, 6, 16, 3, 50, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(prior_today_latitude),
                        longitude=str(prior_today_longitude),
                        odometer_miles=Decimal("10.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 5, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(reset_point_latitude),
                        longitude=str(reset_point_longitude),
                        odometer_miles=Decimal("20000.0"),
                    ),
                    _location(
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        datetime(2026, 6, 16, 16, 0, tzinfo=UTC),
                        {"_type": "location"},
                        latitude=str(end_point_latitude),
                        longitude=str(end_point_longitude),
                        odometer_miles=Decimal("20003.0"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        assert f"<strong>{today_total}</strong>" in response.text
        assert f"<strong>{month_total}</strong>" in response.text
        assert "<strong>19993.0</strong>" not in response.text
        assert "<strong>19998.0</strong>" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_replaces_vehicle_mpg_with_location_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "trip_tracker.web.routes._monthly_gas_context",
        lambda _db, _year, _month: (None, ""),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            waypoint = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            db.add(waypoint)
            db.add(
                _location(
                    datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                    datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                    {"_type": "location", "inregions": ["Home"]},
                    latitude="42.3314000",
                    longitude="-83.0458000",
                )
            )
            db.commit()

        response = client.get("/dashboard/content")

        assert response.status_code == 200
        assert "Vehicle MPG" not in response.text
        assert "Location State" in response.text
        assert "Inside waypoint" in response.text
        assert "Home" in response.text
        assert response.text.index("<span>Location State</span>") < response.text.index(
            '<section class="distance-grid"'
        )
    finally:
        app.dependency_overrides.clear()


def test_web_login_temporarily_locks_repeated_failures(monkeypatch) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=2,
        web_login_lockout_seconds=300,
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        for _ in range(2):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/trips?year=2026&month=6",
                },
            )
            assert response.status_code == 200

        locked_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/trips?year=2026&month=6",
            },
        )

        assert locked_response.status_code == 200
        assert "Login is temporarily unavailable." in locked_response.text
        with session_factory() as db:
            payloads = list(
                db.scalars(select(WebLoginAudit).order_by(WebLoginAudit.id.asc()))
            )
            payloads = [audit.payload for audit in payloads]
        assert [payload["reason"] for payload in payloads] == [
            "invalid_credentials",
            "invalid_credentials",
            "locked_out",
        ]
        assert payloads[-1]["lockout_applied"] is True
        assert payloads[-1]["password_length"] == len("secret-password")
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_web_login_auto_blocks_cloudflare_ip_after_five_consecutive_failures(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=10,
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
        cloudflare_auto_block_failed_login_attempts=5,
    )
    created_blocks: list[str] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        created_blocks.append(ip_address)
        assert "5 consecutive failed web login attempts" in note
        return CloudflareAccessRule(rule_id="cf-rule-1", ip_address=ip_address)

    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session(client_host="172.18.0.5")
    try:
        for _ in range(5):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/diagnostics",
                },
                headers={"CF-Connecting-IP": "198.51.100.55"},
            )
            assert response.status_code == 200

        assert created_blocks == ["198.51.100.55"]
        with session_factory() as db:
            block = db.scalar(select(CloudflareIPBlock))
            assert block is not None
            assert block.ip_address == "198.51.100.55"
            assert block.cloudflare_rule_id == "cf-rule-1"
            assert block.source == "automatic"
            assert block.reason == "5 consecutive failed web login attempts"
            assert block.failure_count == 5
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_cloudflare_header_controls_auto_block_when_present(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=10,
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
        cloudflare_auto_block_failed_login_attempts=2,
    )
    created_blocks: list[str] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        created_blocks.append(ip_address)
        assert "2 consecutive failed web login attempts" in note
        return CloudflareAccessRule(rule_id="cf-rule-1", ip_address=ip_address)

    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session(client_host="203.0.113.88")
    try:
        for _ in range(2):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/diagnostics",
                },
                headers={"CF-Connecting-IP": "198.51.100.55"},
            )
            assert response.status_code == 200

        assert created_blocks == ["198.51.100.55"]
        with session_factory() as db:
            blocks = list(db.scalars(select(CloudflareIPBlock).order_by(CloudflareIPBlock.id)))
            assert [block.ip_address for block in blocks] == ["198.51.100.55"]
            assert all(
                block.reason == "2 consecutive failed web login attempts" for block in blocks
            )
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_successful_web_login_resets_consecutive_failures_before_auto_block(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        web_login_max_attempts=10,
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
        cloudflare_auto_block_failed_login_attempts=5,
    )

    def fail_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        raise AssertionError(f"unexpected block for {ip_address}: {note}")

    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes.create_cloudflare_ip_block",
        fail_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session(client_host="172.18.0.5")
    try:
        for _ in range(4):
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "wrong-password",
                    "next_url": "/diagnostics",
                },
                headers={"CF-Connecting-IP": "198.51.100.56"},
            )
            assert response.status_code == 200

        success_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            headers={"CF-Connecting-IP": "198.51.100.56"},
            follow_redirects=False,
        )
        assert success_response.status_code == 303

        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "next_url": "/diagnostics",
            },
            headers={"CF-Connecting-IP": "198.51.100.56"},
        )
        assert response.status_code == 200
        with session_factory() as db:
            assert db.scalar(select(CloudflareIPBlock)) is None
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_cloudflare_ip_block_allowlist_matches_ips_and_cidrs() -> None:
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_block_allowlist="198.51.100.20, 203.0.113.0/24",
    )

    assert ip_is_allowlisted("198.51.100.20", settings)
    assert ip_is_allowlisted("203.0.113.44", settings)
    assert not ip_is_allowlisted("198.51.100.21", settings)


def test_create_cloudflare_ip_block_sends_zone_access_rule_payload(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
    )
    captured_request = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "result": {"id": "rule-123"}}

    def fake_post(url, *, headers, json, timeout):
        captured_request["url"] = url
        captured_request["headers"] = headers
        captured_request["json"] = json
        captured_request["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("trip_tracker.services.cloudflare_blocks.httpx.post", fake_post)

    result = create_cloudflare_ip_block(
        "203.0.113.44",
        note="Trip Tracker manual block",
        settings=settings,
    )

    assert result.rule_id == "rule-123"
    assert captured_request["url"].endswith(
        "/zones/test-zone/firewall/access_rules/rules"
    )
    assert captured_request["headers"]["Authorization"] == "Bearer test-token"
    assert captured_request["json"] == {
        "mode": "block",
        "configuration": {"target": "ip", "value": "203.0.113.44"},
        "notes": "Trip Tracker manual block",
    }


def test_create_cloudflare_ip_block_explains_authentication_error(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="bad-token",
        cloudflare_zone_id="test-zone",
    )

    class FakeResponse:
        status_code = 403

        @staticmethod
        def json():
            return {
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
            }

    def fake_post(url, *, headers, json, timeout):
        return FakeResponse()

    monkeypatch.setattr("trip_tracker.services.cloudflare_blocks.httpx.post", fake_post)

    try:
        create_cloudflare_ip_block(
            "203.0.113.44",
            note="Trip Tracker manual block",
            settings=settings,
        )
    except CloudflareBlockError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected CloudflareBlockError for rejected credentials.")

    assert "Cloudflare rejected the API credentials" in message
    assert "CLOUDFLARE_API_TOKEN" in message
    assert "CLOUDFLARED_TUNNEL_TOKEN" in message


def test_waypoints_page_paginates_twenty_per_page() -> None:
    client, session_factory = _test_client_session()
    try:
        created_start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        with session_factory() as db:
            waypoints = []
            for index in range(45):
                waypoints.append(
                    Site(
                        name=f"Waypoint {index:02d}",
                        owntracks_region_id=f"region-{index:02d}",
                        latitude=Decimal("42.3314000"),
                        longitude=Decimal("-83.0458000"),
                        radius_m=150,
                        created_at=created_start + timedelta(minutes=index),
                        last_visited_at=(
                            datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
                            if index == 20
                            else None
                        ),
                    )
                )
            db.add_all(waypoints)
            db.commit()

        first_page_response = client.get("/waypoints?page=1")
        response = client.get("/waypoints?page=2")

        assert first_page_response.status_code == 200
        assert "Waypoint 20" in first_page_response.text
        assert "2026-06-11" in first_page_response.text
        assert response.status_code == 200
        assert "Showing 21-40" in response.text
        assert "of 45" in response.text
        assert "Page 2 of 3" in response.text
        assert '<section id="waypoints-list" class="panel">' in response.text
        assert 'class="pagination-controls waypoint-pagination"' in response.text
        assert 'class="pagination-button-row"' in response.text
        assert 'class="pagination-status-text">Page 2 of 3</span>' in response.text
        assert 'data-pagination-target="waypoints-list"' in response.text
        assert "OwnTracks Region ID" not in response.text
        assert "region-25" not in response.text
        assert "/waypoints?page=1" in response.text
        assert "/waypoints?page=3" in response.text
        assert "Waypoint 25" in response.text
        assert "Waypoint 06" in response.text
        assert "Waypoint 26" not in response.text
        assert "Waypoint 20" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_waypoints_page_deletes_waypoint_and_preserves_trip_history() -> None:
    client, session_factory = _test_client_session()
    trip_date = datetime(2026, 6, 11, 12, 0, tzinfo=UTC).date()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=trip_date,
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 11, 12, 30, tzinfo=UTC),
                    start_latitude=home.latitude,
                    start_longitude=home.longitude,
                    end_latitude=client_site.latitude,
                    end_longitude=client_site.longitude,
                    miles=Decimal("12.34"),
                    source="auto",
                )
            )
            waypoint_id = client_site.id
            db.commit()

        page_response = client.get("/waypoints")
        delete_response = client.post(
            f"/waypoints/{waypoint_id}/delete",
            data={"page": "1"},
        )

        assert page_response.status_code == 200
        assert f"/waypoints/{waypoint_id}/delete" in page_response.text
        assert "Delete" in page_response.text
        assert delete_response.status_code == 200
        assert "Client" not in delete_response.text
        with session_factory() as db:
            assert db.get(Site, waypoint_id) is None
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.destination_site_id is None
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("12.3")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_uses_console_logging_without_app_log_panel() -> None:
    client, _ = _test_client_session()
    try:
        response = client.get("/diagnostics")
        download_response = client.get("/diagnostics/logs/app")

        assert response.status_code == 200
        assert "App Version" in response.text
        assert __version__ in response.text
        assert "Console only" in response.text
        assert "App Log" not in response.text
        assert "Refresh App Log" not in response.text
        assert "Download App Log" not in response.text
        assert download_response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_failed_login_attempts_without_footer_actions(
    monkeypatch,
) -> None:
    success_payload = {
        "event": "web_login_succeeded",
        "occurred_at_utc": "2026-06-19T11:59:00Z",
        "occurred_at_local": "2026-06-19T07:59:00-04:00",
        "client_ip": "203.0.113.9",
        "direct_client_ip": "10.0.0.11",
        "cf_connecting_ip": "203.0.113.9",
        "x_real_ip": "",
        "x_forwarded_for": "",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "SuccessBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "account": "admin",
    }
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.10",
        "direct_client_ip": "10.0.0.12",
        "x_real_ip": "203.0.113.10",
        "x_forwarded_for": "203.0.113.10, 10.0.0.12",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    monkeypatch.setattr(
        "trip_tracker.web.routes.get_settings",
        lambda: Settings(database_url="sqlite://"),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            _store_login_audit(db, success_payload, entry_id="1" * 64)
            _store_login_audit(db, payload, entry_id="2" * 64)
        response = client.get("/diagnostics")
        download_response = client.get("/diagnostics/logs/login-failures")

        assert response.status_code == 200
        success_section = _html_section(
            response.text,
            '<section id="login-successes" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Successful Login Attempts" in success_section
        assert "203.0.113.9" in success_section
        assert "SuccessBrowser/1.0" in success_section
        assert "<th>Account</th>" not in success_section
        assert "<th>Method</th>" in success_section
        assert "Password" in success_section
        assert "admin" in success_section
        assert "Failed Login Attempts" in response.text
        assert "203.0.113.10" in response.text
        assert "admin" in response.text
        assert "ExampleBrowser/1.0" in response.text
        assert "Refresh Login Failures" not in response.text
        assert "Download Login Failure Log" not in response.text
        assert "/diagnostics/logs/login-failures" not in response.text
        assert download_response.status_code == 200
        assert "web_login_failed" in download_response.text
        assert "attachment" in download_response.headers["content-disposition"]
        assert "trip-tracker-login-audits.jsonl" in download_response.headers[
            "content-disposition"
        ]
        assert download_response.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_paginates_failed_logins_and_cloudflare_blocks(
    monkeypatch,
) -> None:
    success_lines = []
    for index in range(12):
        success_lines.append(
            json.dumps(
                {
                    "event": "web_login_succeeded",
                    "occurred_at_utc": f"2026-06-19T11:{index:02d}:00Z",
                    "occurred_at_local": f"2026-06-19T07:{index:02d}:00-04:00",
                    "client_ip": f"198.51.100.{50 + index}",
                    "direct_client_ip": "10.0.0.12",
                    "cf_connecting_ip": f"198.51.100.{50 + index}",
                    "x_real_ip": "",
                    "x_forwarded_for": "",
                    "forwarded_proto": "https",
                    "host": "mileage.example.test",
                    "user_agent": f"SuccessBrowser/{index}",
                    "method": "POST",
                    "path": "/login",
                    "next_url": "/diagnostics",
                    "username": f"success-{index:02d}",
                    "username_length": 10,
                    "username_truncated": False,
                    "account": "admin",
                }
            )
        )
    lines = []
    for index in range(12):
        lines.append(
            json.dumps(
                {
                    "event": "web_login_failed",
                    "occurred_at_utc": f"2026-06-19T12:{index:02d}:00Z",
                    "occurred_at_local": f"2026-06-19T08:{index:02d}:00-04:00",
                    "client_ip": f"203.0.113.{100 + index}",
                    "direct_client_ip": "10.0.0.12",
                    "cf_connecting_ip": f"203.0.113.{100 + index}",
                    "x_real_ip": "",
                    "x_forwarded_for": "",
                    "forwarded_proto": "https",
                    "host": "mileage.example.test",
                    "user_agent": f"ExampleBrowser/{index}",
                    "method": "POST",
                    "path": "/login",
                    "next_url": "/diagnostics",
                    "reason": "invalid_credentials",
                    "username": f"user-{index:02d}",
                    "username_length": 7,
                    "username_truncated": False,
                    "password_length": 14,
                    "failed_count": index + 1,
                    "max_attempts": 5,
                    "lockout_applied": False,
                    "lockout_remaining_seconds": 0,
                }
            )
        )
    monkeypatch.setattr(
        "trip_tracker.web.routes.get_settings",
        lambda: Settings(database_url="sqlite://"),
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            for index, line in enumerate(success_lines):
                _store_login_audit(
                    db,
                    json.loads(line),
                    entry_id=f"{index + 1:064x}",
                )
            for index, line in enumerate(lines):
                _store_login_audit(
                    db,
                    json.loads(line),
                    entry_id=f"{index + 101:064x}",
                )
            for index in range(12):
                db.add(
                    CloudflareIPBlock(
                        ip_address=f"198.51.100.{100 + index}",
                        cloudflare_rule_id=f"cf-rule-{index:02d}",
                        source="automatic" if index == 11 else "manual",
                        reason=(
                            "5 consecutive failed web login attempts"
                            if index == 11
                            else f"test block {index:02d}"
                        ),
                        created_at=datetime(2026, 6, 19, 12, index, tzinfo=UTC),
                    )
                )
            db.commit()

        response = client.get("/diagnostics")
        assert response.status_code == 200
        assert response.text.count('class="pagination-controls diagnostics-pagination"') == 5
        assert response.text.count('class="pagination-button-row"') >= 5
        assert response.text.count('class="pagination-status-text">Page 1 of 2</span>') >= 3

        success_section = _html_section(
            response.text,
            '<section id="login-successes" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Showing 1-10 of 12 stored in PostgreSQL" in success_section
        assert 'class="pagination-button-row"' in success_section
        assert 'class="pagination-status-text">Page 1 of 2</span>' in success_section
        assert 'data-pagination-target="login-successes"' in success_section
        assert success_section.count("<tr>") == 11
        assert "success-11" in success_section
        assert "success-02" in success_section
        assert "success-01" not in success_section
        assert "success-00" not in success_section

        second_success_page = client.get("/diagnostics?login_successes_page=2")
        second_success_section = _html_section(
            second_success_page.text,
            '<section id="login-successes" class="panel">',
            '<section id="login-failures" class="panel">',
        )
        assert "Showing 11-12 of 12 stored in PostgreSQL" in second_success_section
        assert "success-01" in second_success_section
        assert "success-00" in second_success_section
        assert "success-02" not in second_success_section

        login_section = _html_section(
            response.text,
            '<section id="login-failures" class="panel">',
            '<section id="cloudflare-blocked-ips" class="panel">',
        )
        assert "Showing 1-10 of 12 stored in PostgreSQL" in login_section
        assert 'class="pagination-button-row"' in login_section
        assert 'class="pagination-status-text">Page 1 of 2</span>' in login_section
        assert 'data-pagination-target="login-failures"' in login_section
        assert login_section.count("<tr>") == 11
        assert "user-11" in login_section
        assert "user-02" in login_section
        assert "user-01" not in login_section
        assert "user-00" not in login_section

        second_login_page = client.get("/diagnostics?login_failures_page=2")
        second_login_section = _html_section(
            second_login_page.text,
            '<section id="login-failures" class="panel">',
            '<section id="cloudflare-blocked-ips" class="panel">',
        )
        assert "Showing 11-12 of 12 stored in PostgreSQL" in second_login_section
        assert "user-01" in second_login_section
        assert "user-00" in second_login_section
        assert "user-02" not in second_login_section

        cloudflare_section = _html_section(
            response.text,
            '<section id="cloudflare-blocked-ips" class="panel">',
            '<section id="data-backup" class="panel">',
        )
        assert "Showing 1-10 of 12 app-managed Cloudflare" in cloudflare_section
        assert 'class="pagination-button-row"' in cloudflare_section
        assert 'class="pagination-status-text">Page 1 of 2</span>' in cloudflare_section
        assert 'data-pagination-target="cloudflare-blocked-ips"' in cloudflare_section
        assert cloudflare_section.count("<tr>") == 11
        assert "198.51.100.111" in cloudflare_section
        assert "198.51.100.102" in cloudflare_section
        assert '<span class="pill block-source-pill automatic">' in cloudflare_section
        assert '<span class="pill block-source-pill manual">' in cloudflare_section
        assert "Auto" in cloudflare_section
        assert "Manual" in cloudflare_section
        assert "5 consecutive failed web login attempts" in cloudflare_section
        assert "198.51.100.101" not in cloudflare_section
        assert "198.51.100.100" not in cloudflare_section

        second_cloudflare_page = client.get("/diagnostics?cloudflare_blocks_page=2")
        second_cloudflare_section = _html_section(
            second_cloudflare_page.text,
            '<section id="cloudflare-blocked-ips" class="panel">',
            '<section id="data-backup" class="panel">',
        )
        assert "Showing 11-12 of 12 app-managed Cloudflare" in second_cloudflare_section
        assert "198.51.100.101" in second_cloudflare_section
        assert "198.51.100.100" in second_cloudflare_section
        assert "198.51.100.102" not in second_cloudflare_section
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_failed_login_block_button_uses_resolved_client_ip(
    monkeypatch,
) -> None:
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-27T12:00:00Z",
        "occurred_at_local": "2026-06-27T08:00:00-04:00",
        "client_ip": "198.51.100.77",
        "direct_client_ip": "172.18.0.5",
        "cf_connecting_ip": "198.51.100.77",
        "x_real_ip": "127.0.0.1",
        "x_forwarded_for": "203.0.113.44, 172.18.0.5",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    settings = Settings(
        database_url="sqlite://",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="token",
        cloudflare_zone_id="zone",
    )
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            _store_login_audit(db, payload)
        response = client.get("/diagnostics")

        assert response.status_code == 200
        login_section = _html_section(
            response.text,
            '<section id="login-failures" class="panel">',
            '<section id="cloudflare-blocked-ips" class="panel">',
        )
        assert "198.51.100.77" in login_section
        assert 'name="ip_address" value="198.51.100.77"' in login_section
        assert 'aria-label="Block IP at Cloudflare"' in login_section
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_passkey_card_lists_registers_and_removes_passkeys(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            passkey = PasskeyCredential(
                credential_id="YWJj",
                user_handle="user-handle",
                username="admin",
                public_key="public-key",
                device_type="multi_device",
                backed_up=True,
                transports=[],
                created_at=datetime(2026, 6, 25, 12, 0, tzinfo=UTC),
            )
            db.add(passkey)
            db.commit()
            passkey_id = passkey.id

        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        diagnostics_response = client.get("/diagnostics")
        options_response = client.post(
            "/diagnostics/passkeys/register/options",
            headers={"Origin": "https://mileage.example"},
        )
        delete_response = client.post(
            f"/diagnostics/passkeys/{passkey_id}/delete",
            follow_redirects=False,
        )

        assert login_response.status_code == 303
        assert diagnostics_response.status_code == 200
        passkey_section = _html_section(
            diagnostics_response.text,
            '<div id="passkeys" class="panel">',
            '<section id="owntracks-state-log" class="panel">',
        )
        assert "Configure Passkey" in passkey_section
        assert "Device sign-in for admin." in passkey_section
        assert "Multi Device" in passkey_section
        assert "Synced" in passkey_section
        assert f"/diagnostics/passkeys/{passkey_id}/delete" in passkey_section
        assert options_response.status_code == 200
        assert options_response.json()["rp"]["id"] == "mileage.example"
        assert delete_response.status_code == 303
        assert delete_response.headers["location"] == "/diagnostics#passkeys"
        with session_factory() as db:
            assert db.get(PasskeyCredential, passkey_id) is None
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_diagnostics_hides_failed_login_entry_without_deleting_audit(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.10",
        "direct_client_ip": "10.0.0.12",
        "cf_connecting_ip": "203.0.113.10",
        "x_real_ip": "",
        "x_forwarded_for": "",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            _store_login_audit(db, payload)
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        with session_factory() as db:
            entry = tail_login_failure_entries(db)[0]
            audit_count_before_hide = db.scalar(select(func.count(WebLoginAudit.id)))

        response = client.post(
            "/diagnostics/login-failures/hide",
            data={
                "entry_id": entry.entry_id,
                "client_ip": entry.client_ip,
                "occurred_at_utc": entry.occurred_at_utc,
            },
            follow_redirects=False,
        )
        diagnostics_response = client.get("/diagnostics")

        assert response.status_code == 303
        assert "203.0.113.10" not in diagnostics_response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(WebLoginAudit.id))) == audit_count_before_hide
            hidden_entry = db.scalar(select(HiddenLoginFailure))
            assert hidden_entry is not None
            assert hidden_entry.entry_id == entry.entry_id
            assert hidden_entry.client_ip == "203.0.113.10"
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_diagnostics_cloudflare_block_buttons_create_and_remove_app_managed_block(
    monkeypatch,
) -> None:
    FAILED_LOGIN_ATTEMPTS.clear()
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-19T12:00:00Z",
        "occurred_at_local": "2026-06-19T08:00:00-04:00",
        "client_ip": "203.0.113.20",
        "direct_client_ip": "10.0.0.12",
        "cf_connecting_ip": "203.0.113.20",
        "x_real_ip": "",
        "x_forwarded_for": "",
        "forwarded_proto": "https",
        "host": "mileage.example.test",
        "user_agent": "ExampleBrowser/1.0",
        "method": "POST",
        "path": "/login",
        "next_url": "/diagnostics",
        "reason": "invalid_credentials",
        "username": "admin",
        "username_length": 5,
        "username_truncated": False,
        "password_length": 14,
        "failed_count": 1,
        "max_attempts": 5,
        "lockout_applied": False,
        "lockout_remaining_seconds": 0,
    }
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
    )
    deleted_rule_ids: list[str] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        assert ip_address == "203.0.113.20"
        assert "Diagnostics failed-login row block button" in note
        return CloudflareAccessRule(rule_id="cf-manual-rule", ip_address=ip_address)

    def fake_delete_cloudflare_ip_block(rule_id: str, *, settings: Settings):
        deleted_rule_ids.append(rule_id)

    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    monkeypatch.setattr(
        "trip_tracker.web.routes.delete_cloudflare_ip_block",
        fake_delete_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            _store_login_audit(db, payload)
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        initial_response = client.get("/diagnostics")
        assert 'aria-label="Block IP at Cloudflare"' in initial_response.text
        assert ">Off</button>" in initial_response.text

        block_response = client.post(
            "/diagnostics/cloudflare-blocks/block",
            data={"ip_address": "203.0.113.20"},
            follow_redirects=False,
        )
        assert block_response.status_code == 303
        with session_factory() as db:
            block = db.scalar(select(CloudflareIPBlock))
            assert block is not None
            assert block.ip_address == "203.0.113.20"
            assert block.cloudflare_rule_id == "cf-manual-rule"
            assert block.source == "manual"

        blocked_response = client.get("/diagnostics")
        assert "Cloudflare Blocked IPs" in blocked_response.text
        assert "cf-manual-rule" in blocked_response.text
        assert 'aria-label="Unblock IP at Cloudflare"' in blocked_response.text
        assert ">On</button>" in blocked_response.text

        unblock_response = client.post(
            "/diagnostics/cloudflare-blocks/unblock",
            data={"ip_address": "203.0.113.20"},
            follow_redirects=False,
        )
        assert unblock_response.status_code == 303
        assert deleted_rule_ids == ["cf-manual-rule"]
        with session_factory() as db:
            assert db.scalar(select(CloudflareIPBlock)) is None
    finally:
        FAILED_LOGIN_ATTEMPTS.clear()
        app.dependency_overrides.clear()


def test_diagnostics_manual_cloudflare_block_form_validates_and_records_reason(
    monkeypatch,
) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        cloudflare_ip_blocking_enabled=True,
        cloudflare_api_token="test-token",
        cloudflare_zone_id="test-zone",
    )
    created_requests: list[tuple[str, str]] = []

    def fake_create_cloudflare_ip_block(ip_address: str, *, note: str, settings: Settings):
        created_requests.append((ip_address, note))
        return CloudflareAccessRule(
            rule_id=f"rule-{ip_address.replace('.', '-')}",
            ip_address=ip_address,
        )

    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "trip_tracker.web.routes.create_cloudflare_ip_block",
        fake_create_cloudflare_ip_block,
    )
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        initial_response = client.get("/diagnostics")
        assert 'class="form-row cloudflare-block-form"' in initial_response.text
        assert 'name="reason"' in initial_response.text
        assert ">Send</button>" in initial_response.text

        invalid_response = client.post(
            "/diagnostics/cloudflare-blocks/block",
            data={
                "ip_address": "not-an-ip",
                "reason": "Manual diagnostics block",
                "result_anchor": "cloudflare-blocked-ips",
            },
            follow_redirects=False,
        )
        assert invalid_response.status_code == 303
        assert invalid_response.headers["location"].endswith("#cloudflare-blocked-ips")
        assert "Cannot+block+an+invalid+IP+address" in invalid_response.headers["location"]
        assert created_requests == []

        missing_reason_response = client.post(
            "/diagnostics/cloudflare-blocks/block",
            data={
                "ip_address": "198.51.100.77",
                "reason": "   ",
                "result_anchor": "cloudflare-blocked-ips",
            },
            follow_redirects=False,
        )
        assert missing_reason_response.status_code == 303
        assert missing_reason_response.headers["location"].endswith(
            "#cloudflare-blocked-ips"
        )
        assert "A+block+reason+is+required" in missing_reason_response.headers["location"]
        assert created_requests == []

        block_response = client.post(
            "/diagnostics/cloudflare-blocks/block",
            data={
                "ip_address": " 198.51.100.77 ",
                "reason": "Manual abuse report",
                "result_anchor": "cloudflare-blocked-ips",
            },
            follow_redirects=False,
        )
        assert block_response.status_code == 303
        assert block_response.headers["location"].endswith("#cloudflare-blocked-ips")
        assert created_requests == [
            (
                "198.51.100.77",
                "Trip Tracker manual block: Manual abuse report",
            )
        ]
        with session_factory() as db:
            block = db.scalar(select(CloudflareIPBlock))
            assert block is not None
            assert block.ip_address == "198.51.100.77"
            assert block.reason == "Manual abuse report"
            assert block.source == "manual"
            assert block.cloudflare_rule_id == "rule-198-51-100-77"

        blocked_response = client.get("/diagnostics")
        assert "198.51.100.77" in blocked_response.text
        assert "Manual abuse report" in blocked_response.text
        assert '<span class="pill block-source-pill manual">' in blocked_response.text
        assert "Manual" in blocked_response.text
        assert "rule-198-51-100-77" in blocked_response.text
        assert 'aria-label="Remove Cloudflare block"' in blocked_response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_full_backup_download_and_restore_round_trip(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        with session_factory() as db:
            _seed_full_backup_data(db)

        diagnostics_response = client.get("/diagnostics")
        backup_response = client.get("/diagnostics/backup")
        payload = json.loads(gzip.decompress(backup_response.content).decode("utf-8"))
        backup_section_start = diagnostics_response.text.index(
            '<section id="data-backup" class="panel">'
        )
        automatic_backups_start = diagnostics_response.text.index(
            '<div id="automatic-backups" class="backup-subsection">',
            backup_section_start,
        )
        manual_backup_start = diagnostics_response.text.index(
            '<div class="backup-subsection manual-backup-subsection">',
            automatic_backups_start,
        )
        backup_header = diagnostics_response.text[
            backup_section_start:automatic_backups_start
        ]
        manual_backup_section = diagnostics_response.text[manual_backup_start:]
        assert diagnostics_response.status_code == 200
        assert "Application database tables and OwnTracks waypoint export." not in backup_header
        assert "Download Full Backup" not in backup_header
        assert automatic_backups_start < manual_backup_start
        assert "Full Backup Download" in manual_backup_section
        assert "Application database tables and OwnTracks waypoint export." in manual_backup_section
        assert "Download Full Backup" in manual_backup_section
        assert manual_backup_section.index("Download Full Backup") < manual_backup_section.index(
            "Upload Restore"
        )
        assert backup_response.status_code == 200
        assert backup_response.headers["cache-control"] == "no-store"
        assert "trip-tracker-full-backup" in backup_response.headers["content-disposition"]
        assert payload["format"] == "trip_tracker.full_backup"
        assert payload["table_counts"]["sites"] == 1
        assert payload["owntracks_waypoints"]["waypoints"][0]["desc"] == "Client"

        with session_factory() as db:
            existing_site = db.scalar(select(Site))
            assert existing_site is not None
            existing_site.name = "Changed Client"
            db.add(
                Site(
                    name="Temporary",
                    latitude=Decimal("40.0000000"),
                    longitude=Decimal("-80.0000000"),
                    radius_m=100,
                )
            )
            db.commit()

        restore_response = client.post(
            "/diagnostics/restore",
            data={"confirmation": "RESTORE"},
            files={
                "backup_file": (
                    "trip-tracker-full-backup.json.gz",
                    backup_response.content,
                    "application/gzip",
                )
            },
        )

        assert restore_response.status_code == 200
        assert "Full database restore completed." in restore_response.text
        assert "rows restored across" in restore_response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
            assert db.scalar(select(func.count(Trip.id))) == 1
            assert db.scalar(select(func.count(DeletedTrip.id))) == 1
            assert db.scalar(select(func.count(OwnTracksLocation.id))) == 1
            assert db.scalar(select(func.count(TripProcessingCheckpoint.id))) == 1
            assert db.scalar(select(func.count(GasPriceSnapshot.id))) == 1
            assert db.scalar(select(func.count(MonthlyGasPrice.id))) == 1
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.miles == Decimal("12.3")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_restores_retained_automatic_backup(monkeypatch, tmp_path) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
        automatic_backup_dir=str(tmp_path / "backups"),
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        with session_factory() as db:
            _seed_full_backup_data(db)
            backup_result = create_automatic_backup(
                db,
                settings.automatic_backup_dir,
                now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
                reason="startup",
            )

        diagnostics_response = client.get("/diagnostics")
        download_response = client.get(
            "/diagnostics/automatic-backups/download",
            params={"filename": backup_result.backup_file.filename},
        )
        assert diagnostics_response.status_code == 200
        assert "Automatic Backups" in diagnostics_response.text
        assert backup_result.backup_file.reason == "startup"
        assert "trip-tracker-auto-backup-startup-20260620-120000Z.json.gz" == (
            backup_result.backup_file.filename
        )
        assert backup_result.backup_file.filename in diagnostics_response.text
        automatic_backup_section = _html_section(
            diagnostics_response.text,
            '<div id="automatic-backups" class="backup-subsection">',
            '<div class="backup-subsection manual-backup-subsection">',
        )
        assert (
            f'class="backup-file-name" title="{backup_result.backup_file.filename}"'
            in automatic_backup_section
        )
        assert ">Confirmation" not in automatic_backup_section
        assert '<span class="pill warning">Startup</span>' in automatic_backup_section
        assert "Type RESTORE to confirm automatic backup restore" in automatic_backup_section
        assert "/diagnostics/automatic-backups/download" in diagnostics_response.text
        assert download_response.status_code == 200
        assert download_response.content == backup_result.backup_file.path.read_bytes()
        assert download_response.headers["cache-control"] == "no-store"
        assert backup_result.backup_file.filename in download_response.headers[
            "content-disposition"
        ]

        with session_factory() as db:
            existing_site = db.scalar(select(Site))
            assert existing_site is not None
            existing_site.name = "Changed Client"
            db.add(
                Site(
                    name="Temporary",
                    latitude=Decimal("40.0000000"),
                    longitude=Decimal("-80.0000000"),
                    radius_m=100,
                )
            )
            db.commit()

        restore_response = client.post(
            "/diagnostics/automatic-backups/restore",
            data={
                "filename": backup_result.backup_file.filename,
                "confirmation": "RESTORE",
            },
        )

        assert restore_response.status_code == 200
        assert "Automatic backup restore completed." in restore_response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
            assert db.scalar(select(func.count(Trip.id))) == 1
    finally:
        app.dependency_overrides.clear()


def test_automatic_backup_retention_keeps_one_day_of_six_hour_and_prior_daily(
    tmp_path,
) -> None:
    db = _session()
    _seed_full_backup_data(db)
    backup_dir = tmp_path / "backups"
    backup_times = [
        datetime(2026, 6, 17, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 18, 22, 0, tzinfo=UTC),
        datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 19, 22, 0, tzinfo=UTC),
        datetime(2026, 6, 20, 4, 0, tzinfo=UTC),
        datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 20, 22, 0, tzinfo=UTC),
    ]
    for backup_time in backup_times:
        create_automatic_backup(db, backup_dir, now=backup_time)

    retained_backups = list_automatic_backup_files(backup_dir)
    retained_filenames = {backup.filename for backup in retained_backups}

    assert len(retained_backups) == 6
    assert "trip-tracker-auto-backup-20260617-160000Z.json.gz" not in retained_filenames
    assert "trip-tracker-auto-backup-20260618-100000Z.json.gz" not in retained_filenames
    assert "trip-tracker-auto-backup-20260618-220000Z.json.gz" in retained_filenames
    assert "trip-tracker-auto-backup-20260619-100000Z.json.gz" not in retained_filenames
    assert "trip-tracker-auto-backup-20260619-220000Z.json.gz" in retained_filenames
    for hour in (4, 10, 16, 22):
        assert f"trip-tracker-auto-backup-20260620-{hour:02d}0000Z.json.gz" in retained_filenames


def test_diagnostics_full_restore_requires_confirmation(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        secret_key=TEST_SECRET_KEY,
        web_login_username="admin",
        web_login_password="secret-password",
    )
    monkeypatch.setattr("trip_tracker.web.auth.get_settings", lambda: settings)
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    try:
        client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret-password",
                "next_url": "/diagnostics",
            },
        )
        with session_factory() as db:
            _seed_full_backup_data(db)

        backup_response = client.get("/diagnostics/backup")
        response = client.post(
            "/diagnostics/restore",
            data={"confirmation": "restore"},
            files={
                "backup_file": (
                    "trip-tracker-full-backup.json.gz",
                    backup_response.content,
                    "application/gzip",
                )
            },
        )

        assert response.status_code == 200
        assert "Type RESTORE to confirm full database restore." in response.text
        with session_factory() as db:
            assert db.scalar(select(func.count(Site.id))) == 1
            assert db.scalar(select(Site.name)) == "Client"
    finally:
        app.dependency_overrides.clear()


def test_login_audit_export_redacts_sensitive_query_values() -> None:
    payload = {
        "event": "web_login_failed",
        "occurred_at_utc": "2026-06-27T12:00:00Z",
        "occurred_at_local": "2026-06-27T08:00:00-04:00",
        "username": "api_key=secret-value",
    }
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            _store_login_audit(db, payload)
        response = client.get("/diagnostics/logs/login-failures")

        assert response.status_code == 200
        assert "api_key=***" in response.text
        assert "secret-value" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_human_duration_since_formats_last_received_age() -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)

    assert _human_duration_since(now - timedelta(minutes=5), now=now) == "5 minutes ago"
    assert _human_duration_since(None, now=now) == "Never"


def test_diagnostics_shows_last_received_owntracks_age() -> None:
    client, session_factory = _test_client_session()
    received_at = datetime.now(UTC) - timedelta(minutes=5)
    try:
        with session_factory() as db:
            db.add(
                _location(
                    datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    received_at,
                    {"_type": "location"},
                )
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert "Last OwnTracks Received" in response.text
        assert "minutes ago" in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_current_waypoint_state() -> None:
    client, session_factory = _test_client_session()
    arrived_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Client",
                    owntracks_region_id="client",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    _location(
                        arrived_at,
                        arrived_at,
                        {"_type": "transition", "event": "enter", "desc": "Client"},
                    ),
                    _location(
                        arrived_at + timedelta(minutes=5),
                        arrived_at + timedelta(minutes=5),
                        {"_type": "location", "inregions": ["client"]},
                    ),
                ]
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert "OwnTracks State" in response.text
        assert "Inside waypoint" in response.text
        assert "Client" in response.text
        assert "Arrived" in response.text
        assert "OwnTracks State Changes" in response.text
        assert "Arrived at waypoint" in response.text
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_paginates_owntracks_entries_and_state_changes() -> None:
    client, session_factory = _test_client_session()
    captured_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            for index in range(12):
                site = Site(
                    name=f"State {index:02d}",
                    owntracks_region_id=f"state-{index:02d}",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
                location = _location(
                    captured_at + timedelta(minutes=index),
                    captured_at + timedelta(minutes=index + 2),
                    {
                        "_type": "transition",
                        "event": "enter",
                        "desc": site.name,
                        "seq": index,
                    },
                )
                location.user = "user"
                location.device = f"device-{index:02d}"
                location.topic = f"owntracks/user/device-{index:02d}"
                db.add(site)
                db.add(location)
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        state_section = _html_section(
            response.text,
            '<section id="owntracks-state-log" class="panel">',
            '<section id="owntracks-entries" class="panel">',
        )
        assert "Showing 1-10 of 12 state changes." in state_section
        assert 'class="pagination-button-row"' in state_section
        assert 'class="pagination-status-text">Page 1 of 2</span>' in state_section
        assert 'data-pagination-target="owntracks-state-log"' in state_section
        assert state_section.count("<tr>") == 11
        assert "State 11" in state_section
        assert "State 02" in state_section
        assert "State 01" not in state_section
        assert "State 00" not in state_section

        second_state_page = client.get("/diagnostics?state_changes_page=2")
        second_state_section = _html_section(
            second_state_page.text,
            '<section id="owntracks-state-log" class="panel">',
            '<section id="owntracks-entries" class="panel">',
        )
        assert "Showing 11-12 of 12 state changes." in second_state_section
        assert "State 01" in second_state_section
        assert "State 00" in second_state_section
        assert "State 02" not in second_state_section

        entries_section = _html_section(
            response.text,
            '<section id="owntracks-entries" class="panel">',
            '<section id="login-successes" class="panel">',
        )
        assert "Showing 1-10 of 12 entries." in entries_section
        assert 'class="pagination-button-row"' in entries_section
        assert 'class="pagination-status-text">Page 1 of 2</span>' in entries_section
        assert 'data-pagination-target="owntracks-entries"' in entries_section
        assert "<th>ID</th>" not in entries_section
        assert "<th>Original</th>" in entries_section
        assert "<th>Received Delay</th>" in entries_section
        assert "<th>Event</th>" in entries_section
        assert "<th>Battery</th>" not in entries_section
        assert "<th>Topic</th>" not in entries_section
        assert "2 min" in entries_section
        assert "Waypoint enter" in entries_section
        assert entries_section.count("<tr>") == 11
        assert "user / device-11" in entries_section
        assert "user / device-02" in entries_section
        assert "user / device-01" not in entries_section
        assert "user / device-00" not in entries_section
        assert "owntracks/user/device-11" not in entries_section

        second_entries_page = client.get("/diagnostics?owntracks_page=2")
        second_entries_section = _html_section(
            second_entries_page.text,
            '<section id="owntracks-entries" class="panel">',
            '<section id="login-successes" class="panel">',
        )
        assert "Showing 11-12 of 12 entries." in second_entries_section
        assert "user / device-01" in second_entries_section
        assert "user / device-00" in second_entries_section
        assert "user / device-02" not in second_entries_section
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_recent_owntracks_entries_show_delay_and_event_labels() -> None:
    client, session_factory = _test_client_session()
    captured_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            location = _location(
                captured_at,
                captured_at + timedelta(seconds=95),
                {"_type": "location"},
            )
            location.user = "user"
            location.device = "phone"
            location.topic = "owntracks/user/phone"
            waypoint_leave = _location(
                captured_at + timedelta(minutes=10),
                captured_at + timedelta(minutes=11),
                {"_type": "transition", "event": "leave", "desc": "Office"},
            )
            db.add_all([location, waypoint_leave])
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        entries_section = _html_section(
            response.text,
            '<section id="owntracks-entries" class="panel">',
            '<section id="login-successes" class="panel">',
        )
        assert "<th>ID</th>" not in entries_section
        assert "Original" in entries_section
        assert "Received Delay" in entries_section
        assert "Location event" in entries_section
        assert "Waypoint leave" in entries_section
        assert "1 min" in entries_section
        assert "owntracks/user/phone" not in entries_section
        assert "Battery" not in entries_section
        assert "Topic" not in entries_section
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_travel_state_change_outside_waypoints(monkeypatch) -> None:
    monkeypatch.setattr(
        "trip_tracker.services.diagnostics.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            owntracks_travel_distance_m=Decimal("50.0"),
        ),
    )
    client, session_factory = _test_client_session()
    start_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            db.add(
                Site(
                    name="Home",
                    owntracks_region_id="home",
                    latitude=Decimal("42.3314000"),
                    longitude=Decimal("-83.0458000"),
                    radius_m=150,
                )
            )
            db.add_all(
                [
                    _location(
                        start_at,
                        start_at,
                        {"_type": "transition", "event": "enter", "desc": "Home"},
                        odometer_miles=Decimal("1000.0"),
                    ),
                    _location(
                        start_at + timedelta(minutes=10),
                        start_at + timedelta(minutes=12),
                        {"_type": "transition", "event": "leave", "desc": "Home"},
                        odometer_miles=Decimal("1000.2"),
                    ),
                    _location(
                        start_at + timedelta(minutes=11),
                        start_at + timedelta(minutes=14),
                        {"_type": "location"},
                        latitude="42.3440000",
                        longitude="-83.0600000",
                        odometer_miles=Decimal("1001.3"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/diagnostics")

        assert response.status_code == 200
        state_section = _html_section(
            response.text,
            '<section id="owntracks-state-log" class="panel">',
            '<section id="owntracks-entries" class="panel">',
        )
        assert "Travel detected" in state_section
        assert "Left waypoint" in state_section
        assert "Home" in state_section
        assert "<th>Original</th>" in state_section
        assert "<th>Source</th>" in state_section
        assert "<th>Received Delay</th>" in state_section
        assert "<th>Duration</th>" in state_section
        assert "<th>Rolling Odometer</th>" in state_section
        assert "<th>Distance</th>" not in state_section
        assert state_section.index("<th>Received Delay</th>") < state_section.index(
            "<th>State</th>"
        )
        assert state_section.index("<th>Source</th>") < state_section.index("<th>Duration</th>")
        assert "1.1 miles" not in state_section
        assert "10 min" in state_section
        assert "1 min" in state_section
        assert "OwnTracks transition" in state_section
        assert "Movement threshold" in state_section
        assert "2 min" in state_section
        assert "3 min" in state_section
        assert "1,000.2 miles" in state_section
        assert "1,001.3 miles" in state_section
    finally:
        app.dependency_overrides.clear()


def test_trips_page_delete_button_removes_trip_and_records_exact_deletion() -> None:
    client, session_factory = _test_client_session()
    trip_date = datetime(2026, 6, 11, 12, 0, tzinfo=UTC).date()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=trip_date,
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 11, 12, 30, tzinfo=UTC),
                    start_latitude=home.latitude,
                    start_longitude=home.longitude,
                    end_latitude=client_site.latitude,
                    end_longitude=client_site.longitude,
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("4.25"),
                    mileage_source="waypoint_distance",
                    source="auto",
                )
            )
            db.commit()

        page_response = client.get("/trips/content?year=2026&month=6")
        delete_response = client.post("/trips/1/delete", follow_redirects=False)
        content_response = client.get("/trips/content?year=2026&month=6")

        assert page_response.status_code == 200
        assert "Delete" in page_response.text
        assert delete_response.status_code == 303
        assert delete_response.headers["location"] == "/trips?year=2026&month=6"
        assert "No work trips for this month." in content_response.text
        with session_factory() as db:
            assert db.get(Trip, 1) is None
            deleted_trip = db.scalar(select(DeletedTrip))
            assert deleted_trip is not None
            assert deleted_trip.origin_name == "Home"
            assert deleted_trip.destination_name == "Client"
            assert deleted_trip.reason == "user_deleted"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_removes_deleted_trip_record() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = Site(
                name="Home",
                latitude=Decimal("42.3314000"),
                longitude=Decimal("-83.0458000"),
                radius_m=150,
            )
            client_site = Site(
                name="Client",
                latitude=Decimal("42.3440000"),
                longitude=Decimal("-83.0600000"),
                radius_m=150,
            )
            db.add_all([home, client_site])
            db.flush()
            db.add(
                DeletedTrip(
                    deleted_trip_id=42,
                    trip_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 15, 12, 30, tzinfo=UTC),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("12.50"),
                    source="auto",
                    mileage_source="owntracks_path",
                    reason="user_deleted",
                )
            )
            db.commit()

        page_response = client.get("/trips/content?year=2026&month=6")
        delete_response = client.post(
            "/trips/suppression/1/delete",
            data={"redirect_year": "2026", "redirect_month": "6"},
            follow_redirects=False,
        )
        content_response = client.get("/trips/content?year=2026&month=6")

        assert page_response.status_code == 200
        assert "Deleted Work Trip Records" in page_response.text
        assert "Remove Record" in page_response.text
        assert "Home" in page_response.text
        assert "Client" in page_response.text
        assert delete_response.status_code == 303
        assert delete_response.headers["location"] == "/trips?year=2026&month=6"
        assert "No deleted work trip records for this month." in content_response.text
        with session_factory() as db:
            assert db.get(DeletedTrip, 1) is None
    finally:
        app.dependency_overrides.clear()


def test_trips_page_creates_manual_trip(monkeypatch) -> None:
    monkeypatch.setattr("trip_tracker.web.routes.local_today", lambda: date(2026, 6, 22))
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.commit()
            home_id = home.id
            client_site_id = client_site.id

        page_response = client.get("/trips/content?year=2026&month=6")
        create_response = client.post(
            "/trips",
            data={
                "trip_date": "2026-06-15",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_site_id),
                "miles": "12.34",
            },
            follow_redirects=False,
        )
        content_response = client.get("/trips/content?year=2026&month=6")

        assert page_response.status_code == 200
        assert "Showing June 2026 (06/2026)" in page_response.text
        assert 'type="month"' in page_response.text
        assert 'value="2026-06"' in page_response.text
        assert "View Month" not in page_response.text
        assert 'href="/trips?year=2026&amp;month=5"' not in page_response.text
        assert 'href="/trips?year=2026&amp;month=7"' not in page_response.text
        assert "Add Work Trip" in page_response.text
        assert page_response.text.index("<h2>Monthly Work Trips</h2>") < page_response.text.index(
            "<h2>Add Work Trip</h2>"
        )
        assert page_response.text.index("<h2>Add Work Trip</h2>") < page_response.text.index(
            "<h2>Extra Report Expenses</h2>"
        )
        extra_expenses_index = page_response.text.index("<h2>Extra Report Expenses</h2>")
        deleted_trips_index = page_response.text.index("<h2>Deleted Work Trip Records</h2>")
        assert extra_expenses_index < deleted_trips_index
        assert 'name="trip_date" value="2026-06-22"' in page_response.text
        assert 'name="origin_site_id"' in page_response.text
        assert 'name="destination_site_id"' in page_response.text
        assert create_response.status_code == 303
        assert create_response.headers["location"] == "/trips?year=2026&month=6"
        assert "2026-06-15" in content_response.text
        assert "Home" in content_response.text
        assert "Client" in content_response.text
        with session_factory() as db:
            trip = db.scalar(select(Trip))
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 15, tzinfo=UTC).date()
            assert trip.origin_site_id == home_id
            assert trip.destination_site_id == client_site_id
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.start_latitude == Decimal("42.3314000")
            assert trip.end_latitude == Decimal("42.3440000")
            assert trip.miles == Decimal("12.3")
            assert trip.source == "manual"
            assert trip.mileage_source == "manual"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_shades_automatic_edited_and_manual_trip_rows() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add_all(
                [
                    Trip(
                        trip_date=date(2026, 6, 10),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        start_latitude=home.latitude,
                        start_longitude=home.longitude,
                        end_latitude=client_site.latitude,
                        end_longitude=client_site.longitude,
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("5.0"),
                        source="auto",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 11),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 11, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 11, 13, 30, tzinfo=UTC),
                        start_latitude=home.latitude,
                        start_longitude=home.longitude,
                        end_latitude=client_site.latitude,
                        end_longitude=client_site.longitude,
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("6.0"),
                        source="manual",
                        notes=MANUAL_TRIP_NOTE,
                    ),
                    Trip(
                        trip_date=date(2026, 6, 12),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=home.latitude,
                        start_longitude=home.longitude,
                        end_latitude=client_site.latitude,
                        end_longitude=client_site.longitude,
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("7.0"),
                        mileage_source="manual",
                        source="auto",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 13),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 13, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 13, 13, 30, tzinfo=UTC),
                        start_latitude=home.latitude,
                        start_longitude=home.longitude,
                        end_latitude=client_site.latitude,
                        end_longitude=client_site.longitude,
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("8.0"),
                        mileage_source="manual",
                        source="manual",
                        notes=WAYPOINT_TRIP_NOTE,
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 200
        assert 'class="trip-row trip-row-manual"' in response.text
        assert 'data-trip-source="manual"' in response.text
        assert response.text.count('class="trip-row trip-row-auto"') == 1
        assert response.text.count('class="trip-row trip-row-edited"') == 2
        assert response.text.count('data-trip-mileage-edited="true"') == 2
        assert "trip-edit-pill" not in response.text
        assert 'aria-label="Trip row color key"' in response.text
        assert response.text.count("<strong>Automatic</strong>") == 1
        assert response.text.count("<strong>Edited</strong>") == 1
        assert response.text.count("<strong>Manual</strong>") == 1
        styles = Path("trip_tracker/web/static/styles.css").read_text()
        assert (
            ".trip-row-auto,\n.deleted-trip-row-auto {\n  background:\n    "
            "linear-gradient(90deg, "
            "rgba(75, 163, 255, 0.11)"
        ) in styles
        assert (
            ".trip-row-edited {\n  background:\n    "
            "linear-gradient(90deg, "
            "rgba(168, 85, 247, 0.13)"
        ) in styles
        assert (
            ".trip-row-manual,\n.deleted-trip-row-manual {\n  background:\n    "
            "linear-gradient(90deg, "
            "rgba(226, 173, 69, 0.13)"
        ) in styles
    finally:
        app.dependency_overrides.clear()


def test_trips_page_shades_deleted_trip_rows_by_source() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add_all(
                [
                    DeletedTrip(
                        deleted_trip_id=101,
                        trip_date=date(2026, 6, 10),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("5.0"),
                        source="auto",
                        mileage_source="owntracks_path",
                        reason="user_deleted",
                    ),
                    DeletedTrip(
                        deleted_trip_id=102,
                        trip_date=date(2026, 6, 11),
                        origin_site_id=home.id,
                        destination_site_id=client_site.id,
                        started_at=datetime(2026, 6, 11, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 11, 13, 30, tzinfo=UTC),
                        origin_name="Home",
                        destination_name="Client",
                        miles=Decimal("6.0"),
                        source="manual",
                        mileage_source="manual",
                        notes=MANUAL_TRIP_NOTE,
                        reason="user_deleted",
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 200
        assert (
            'class="deleted-trip-row deleted-trip-row-auto" data-trip-source="auto"'
            in response.text
        )
        assert (
            'class="deleted-trip-row deleted-trip-row-manual" data-trip-source="manual"'
            in response.text
        )
    finally:
        app.dependency_overrides.clear()


def test_trips_page_creates_and_deletes_monthly_report_expense() -> None:
    client, session_factory = _test_client_session()
    try:
        page_response = client.get("/trips/content?year=2026&month=6")
        create_response = client.post(
            "/trips/report-expenses/add",
            data={
                "expense_date": "2026-06-18",
                "expense_reason": "Parking and tolls",
                "expense_amount": "12.345",
            },
            follow_redirects=False,
        )
        content_response = client.get("/trips/content?year=2026&month=6")
        delete_response = client.post(
            "/trips/report-expenses/1/delete",
            data={"redirect_year": "2026", "redirect_month": "6"},
            follow_redirects=False,
        )
        final_response = client.get("/trips/content?year=2026&month=6")

        assert page_response.status_code == 200
        assert "Extra Report Expenses" in page_response.text
        assert "No extra report expenses for this month." in page_response.text
        assert create_response.status_code == 303
        assert create_response.headers["location"] == "/trips?year=2026&month=6"
        assert "Parking and tolls" in content_response.text
        assert "$12.35" in content_response.text
        assert "1/5 expenses" in content_response.text
        assert delete_response.status_code == 303
        assert delete_response.headers["location"] == "/trips?year=2026&month=6"
        assert "No extra report expenses for this month." in final_response.text
        with session_factory() as db:
            assert db.scalar(select(MonthlyReportExpense)) is None
    finally:
        app.dependency_overrides.clear()


def test_trips_page_enforces_monthly_report_expense_cap() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            for index in range(5):
                db.add(
                    MonthlyReportExpense(
                        expense_date=date(2026, 6, index + 1),
                        year=2026,
                        month=6,
                        reason=f"Expense {index}",
                        amount=Decimal("1.00"),
                    )
                )
            db.commit()

        page_response = client.get("/trips/content?year=2026&month=6")
        create_response = client.post(
            "/trips/report-expenses/add",
            data={
                "expense_date": "2026-06-18",
                "expense_reason": "Sixth expense",
                "expense_amount": "2.00",
            },
            follow_redirects=False,
        )

        assert page_response.status_code == 200
        assert "5/5 expenses" in page_response.text
        assert "maximum of 5 extra expenses" in page_response.text
        assert create_response.status_code == 400
        with session_factory() as db:
            assert db.scalar(select(func.count(MonthlyReportExpense.id))) == 5
    finally:
        app.dependency_overrides.clear()


def test_trips_page_shows_selected_month_summary_cards() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    _location(
                        datetime(2026, 6, 5, 13, 0, tzinfo=UTC),
                        datetime(2026, 6, 5, 13, 0, tzinfo=UTC),
                        {"_type": "location"},
                    ),
                    _location(
                        datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
                        datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
                        {"_type": "transition", "event": "enter"},
                    ),
                    _location(
                        datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
                        datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
                        {"_type": "location"},
                    ),
                    Trip(
                        trip_date=date(2026, 6, 12),
                        started_at=datetime(2026, 6, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("25.0"),
                    ),
                    Trip(
                        trip_date=date(2026, 6, 16),
                        started_at=datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 16, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("77.4"),
                    ),
                    Trip(
                        trip_date=date(2026, 7, 2),
                        started_at=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 7, 2, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("8.0"),
                    ),
                    MonthlyGasPrice(
                        year=2026,
                        month=6,
                        state="MI",
                        average_price_per_gallon=Decimal("3.500"),
                        buffer_per_gallon=Decimal("0.00"),
                        effective_rate=Decimal("3.500"),
                        source="test",
                        source_detail="trips summary test",
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 200
        summary_section = _html_section(
            response.text,
            '<section class="trips-summary-grid"',
            '<section class="panel">',
        )
        assert response.text.index('<section class="trips-summary-grid"') < response.text.index(
            "<h2>Monthly Work Trips</h2>"
        )
        assert "Month Work Trips + Non-Work Trips" in summary_section
        assert "Month Work Trips Only" in summary_section
        assert "OwnTracks Events" in summary_section
        assert "Month Reimbursement" in summary_section
        assert "Monthly Avg Gas" in summary_section
        assert "<strong>102.4</strong>" in summary_section
        assert "<strong>2</strong>" in summary_section
        assert "$14.34" in summary_section
        assert "4.0 reimbursement gallons" in summary_section
        assert "$3.500" in summary_section
        assert "MI regular" in summary_section
    finally:
        app.dependency_overrides.clear()


def test_trips_top_cards_format_large_numbers_with_commas() -> None:
    rendered = templates.env.get_template("trips_content.html").render(
        {
            "selected_month_display": "Showing June 2026 (06/2026)",
            "selected_month_value": "2026-06",
            "year": 2026,
            "month": 6,
            "today": date(2026, 6, 16),
            "expense_default_date": date(2026, 6, 16),
            "monthly_report_expenses": [],
            "monthly_report_expense_total": Decimal("0.00"),
            "monthly_report_expense_limit": 5,
            "monthly_report_expense_slots_remaining": 5,
            "trips_summary": {
                "month_total": Decimal("123456.7"),
                "month_trips": Decimal("23456.7"),
                "month_non_trips": Decimal("100000.0"),
                "owntracks_event_count": 12_345,
                "trip_count": 1_234,
                "reimbursement": {
                    "total": Decimal("12345.67"),
                    "mileage_total": Decimal("12345.67"),
                    "expense_total": Decimal("0.00"),
                    "total_miles": Decimal("98765.4"),
                    "reimbursement_gallons": Decimal("1234.56"),
                    "reimbursement_gallons_display": "1234.5",
                },
                "monthly_gas": SimpleNamespace(
                    state="MI",
                    average_price_per_gallon=Decimal("1234.567"),
                ),
                "monthly_gas_message": "",
            },
            "waypoints": [],
            "waypoint_names": [],
            "trips": [],
            "suppressed_trips": [],
        }
    )

    summary_section = _html_section(
        rendered,
        '<section class="trips-summary-grid"',
        '<section class="panel">',
    )
    assert "<strong>123,456.7</strong>" in summary_section
    assert "<strong>23,456.7</strong>" in summary_section
    assert "<strong>12,345</strong>" in summary_section
    assert "<strong>1,234</strong>" in summary_section
    assert "$12,345.67" in summary_section
    assert "1,234.5 reimbursement gallons" in summary_section
    assert "$1,234.567" in summary_section


def test_trips_page_uses_owntracks_monthly_summary_after_raw_rows_are_purged() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    OwnTracksMonthlySummary(
                        year=2026,
                        month=3,
                        total_miles=Decimal("18.4"),
                        event_count=42,
                    ),
                    Trip(
                        trip_date=date(2026, 3, 12),
                        started_at=datetime(2026, 3, 12, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 3, 12, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.0"),
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips/content?year=2026&month=3")

        assert response.status_code == 200
        summary_section = _html_section(
            response.text,
            '<section class="trips-summary-grid"',
            '<section class="panel">',
        )
        assert "Showing March 2026 (03/2026)" in response.text
        assert "<strong>18.4</strong>" in summary_section
        assert "<strong>42</strong>" in summary_section
        assert "<strong>5.0</strong>" in summary_section
    finally:
        app.dependency_overrides.clear()


def test_trips_page_lists_newest_trips_first() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            db.add_all(
                [
                    Trip(
                        trip_date=date(2026, 6, 10),
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="First Stop",
                        destination_name="First Client",
                        miles=Decimal("5.0"),
                        source="manual",
                    ),
                    Trip(
                        trip_date=date(2026, 6, 10),
                        started_at=datetime(2026, 6, 10, 16, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 16, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="Second Stop",
                        destination_name="Second Client",
                        miles=Decimal("7.0"),
                        source="manual",
                    ),
                ]
            )
            db.commit()

        response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 200
        assert response.text.index("trip-form-2") < response.text.index("trip-form-1")
    finally:
        app.dependency_overrides.clear()


def test_trips_page_updates_existing_trip_distance_without_editing_date() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            old_home = _site("Old Home", "42.3314000", "-83.0458000")
            old_client = _site("Old Client", "42.3440000", "-83.0600000")
            new_home = _site("New Home", "42.3600000", "-83.0700000")
            new_client = _site("New Client", "42.3700000", "-83.0800000")
            db.add_all([old_home, old_client, new_home, new_client])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    origin_site_id=old_home.id,
                    destination_site_id=old_client.id,
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("42.3314"),
                    start_longitude=Decimal("-83.0458"),
                    end_latitude=Decimal("42.3440"),
                    end_longitude=Decimal("-83.0600"),
                    origin_name="Old Home",
                    destination_name="Old Client",
                    miles=Decimal("5.00"),
                    start_odometer_miles=Decimal("1000.000"),
                    end_odometer_miles=Decimal("1005.000"),
                    start_odometer_source="estimated",
                    end_odometer_source="estimated",
                    source="auto",
                )
            )
            db.commit()
            new_home_id = new_home.id
            new_client_id = new_client.id

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-16",
                "origin_site_id": str(new_home_id),
                "destination_site_id": str(new_client_id),
                "miles": "15.50",
                "start_odometer_miles": "2000.1234",
                "end_odometer_miles": "2015.9876",
            },
            follow_redirects=False,
        )
        content_response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 303
        assert response.headers["location"] == "/trips?year=2026&month=6"
        assert "2026-06-10" in content_response.text
        assert "2026-06-16" not in content_response.text
        assert "New Home" in content_response.text
        assert "New Client" in content_response.text
        assert 'class="trip-row trip-row-edited"' in content_response.text
        assert 'data-trip-mileage-edited="true"' in content_response.text
        assert "trip-edit-pill" not in content_response.text
        with session_factory() as db:
            trip = db.get(Trip, 1)
            assert trip is not None
            assert trip.trip_date == datetime(2026, 6, 10, tzinfo=UTC).date()
            assert trip.origin_site_id == new_home_id
            assert trip.destination_site_id == new_client_id
            assert trip.origin_name == "New Home"
            assert trip.destination_name == "New Client"
            assert trip.start_latitude == Decimal("42.3600000")
            assert trip.end_latitude == Decimal("42.3700000")
            assert trip.miles == Decimal("15.50")
            assert trip.start_odometer_miles == Decimal("1000.000")
            assert trip.end_odometer_miles == Decimal("1015.500")
            assert trip.start_odometer_source == "previous_trip"
            assert trip.end_odometer_source == "estimated"
            assert trip.source == "auto"
            assert trip.mileage_source == "manual"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_odometer_values_are_read_only() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    origin_site_id=home.id,
                    destination_site_id=client_site.id,
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("42.3314"),
                    start_longitude=Decimal("-83.0458"),
                    end_latitude=Decimal("42.3440"),
                    end_longitude=Decimal("-83.0600"),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("5.00"),
                    mileage_source="owntracks_path",
                    source="auto",
                )
            )
            db.commit()
            home_id = home.id
            client_site_id = client_site.id

        page_response = client.get("/trips/content?year=2026&month=6")
        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_site_id),
                "miles": "5.00",
                "start_odometer_miles": "3000.111",
                "end_odometer_miles": "",
            },
            follow_redirects=False,
        )

        assert page_response.status_code == 200
        assert '<span class="trip-date">2026-06-10</span>' in page_response.text
        assert 'class="date-input" type="date" name="trip_date"' not in page_response.text
        assert 'name="origin_site_id"' in page_response.text
        assert 'name="destination_site_id"' in page_response.text
        assert f'value="{home_id}" selected' in page_response.text
        assert f'value="{client_site_id}" selected' in page_response.text
        assert 'name="origin_name" maxlength="160" required value="Home"' not in page_response.text
        assert (
            'name="destination_name" maxlength="160" required value="Client"'
            not in page_response.text
        )
        assert 'name="start_odometer_miles"' not in page_response.text
        assert 'name="end_odometer_miles"' not in page_response.text
        assert response.status_code == 303
        assert response.headers["location"] == "/trips?year=2026&month=6"
        with session_factory() as db:
            trip = db.get(Trip, 1)
            assert trip is not None
            assert trip.origin_site_id == home_id
            assert trip.destination_site_id == client_site_id
            assert trip.origin_name == "Home"
            assert trip.destination_name == "Client"
            assert trip.miles == Decimal("5.00")
            assert trip.start_odometer_miles is None
            assert trip.end_odometer_miles is None
            assert trip.source == "auto"
            assert trip.mileage_source == "owntracks_path"
    finally:
        app.dependency_overrides.clear()


def test_trips_page_waypoint_dropdowns_preselect_matching_trip_names() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_site = _site("Client", "42.3440000", "-83.0600000")
            db.add_all([home, client_site])
            db.flush()
            db.add(
                Trip(
                    trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                    started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("0.0000000"),
                    start_longitude=Decimal("0.0000000"),
                    end_latitude=Decimal("0.0000000"),
                    end_longitude=Decimal("0.0000000"),
                    origin_name="Home",
                    destination_name="Client",
                    miles=Decimal("5.0"),
                    source="manual",
                )
            )
            db.commit()
            home_id = home.id
            client_site_id = client_site.id

        response = client.get("/trips/content?year=2026&month=6")

        assert response.status_code == 200
        assert f'<option value="{home_id}" selected>Home</option>' in response.text
        assert f'<option value="{client_site_id}" selected>Client</option>' in response.text
    finally:
        app.dependency_overrides.clear()


def test_trips_page_distance_edit_resequences_month_odometers() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home", "42.3314000", "-83.0458000")
            client_a = _site("Client A", "42.3440000", "-83.0600000")
            client_b = _site("Client B", "42.3600000", "-83.0700000")
            db.add_all([home, client_a, client_b])
            db.flush()
            db.add_all(
                [
                    Trip(
                        trip_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
                        origin_site_id=home.id,
                        destination_site_id=client_a.id,
                        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 10, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        origin_name="Home",
                        destination_name="Client A",
                        miles=Decimal("5.00"),
                        start_odometer_miles=Decimal("1000.000"),
                        end_odometer_miles=Decimal("1005.000"),
                        source="auto",
                    ),
                    Trip(
                        trip_date=datetime(2026, 6, 11, tzinfo=UTC).date(),
                        origin_site_id=client_a.id,
                        destination_site_id=client_b.id,
                        started_at=datetime(2026, 6, 11, 13, 0, tzinfo=UTC),
                        ended_at=datetime(2026, 6, 11, 13, 30, tzinfo=UTC),
                        start_latitude=Decimal("42.3440"),
                        start_longitude=Decimal("-83.0600"),
                        end_latitude=Decimal("42.3600"),
                        end_longitude=Decimal("-83.0700"),
                        origin_name="Client A",
                        destination_name="Client B",
                        miles=Decimal("7.00"),
                        start_odometer_miles=Decimal("1005.000"),
                        end_odometer_miles=Decimal("1012.000"),
                        source="auto",
                    ),
                ]
            )
            db.commit()
            home_id = home.id
            client_a_id = client_a.id

        response = client.post(
            "/trips/1",
            data={
                "trip_date": "2026-06-10",
                "origin_site_id": str(home_id),
                "destination_site_id": str(client_a_id),
                "miles": "6.25",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/trips?year=2026&month=6"
        with session_factory() as db:
            trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
            assert trips[0].start_odometer_miles == Decimal("1000.0")
            assert trips[0].end_odometer_miles == Decimal("1006.3")
            assert trips[1].start_odometer_miles == Decimal("1006.3")
            assert trips[1].end_odometer_miles == Decimal("1013.3")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_manual_odometer_form_saves_reading_at_home() -> None:
    client, session_factory = _test_client_session()
    try:
        with session_factory() as db:
            home = _site("Home")
            db.add(home)
            db.flush()
            db.add(
                OwnTracksLocation(
                    captured_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                    received_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                    latitude=home.latitude,
                    longitude=home.longitude,
                    raw_payload={"_type": "location", "inregions": ["Home"]},
                )
            )
            db.commit()

        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "12345.678"},
        )

        assert response.status_code == 200
        assert "Manual Odometer" in response.text
        assert "Pass" in response.text
        assert "12,345.7 miles" in response.text
        assert "Manual" in response.text
        with session_factory() as db:
            checkpoint = db.scalar(
                select(TripProcessingCheckpoint).where(
                    TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT
                )
            )
            assert checkpoint is not None
            assert checkpoint.odometer_anchor_miles == Decimal("12345.7")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_manual_odometer_rejects_save_away_from_home() -> None:
    client, session_factory = _test_client_session()
    try:
        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "12345.6"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "only+be+saved+while+inside+the+Home+waypoint" in response.headers["location"]
        with session_factory() as db:
            assert db.scalar(select(TripProcessingCheckpoint)) is None
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_emergency_rebuild_backs_up_then_repairs_away_from_home(
    monkeypatch,
    tmp_path,
) -> None:
    settings = Settings(
        database_url="sqlite://",
        automatic_backup_dir=str(tmp_path / "backups"),
    )
    monkeypatch.setattr("trip_tracker.web.routes.get_settings", lambda: settings)
    client, session_factory = _test_client_session()
    first_start = datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
    second_start = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            db.add_all(
                [
                    Trip(
                        trip_date=first_start.date(),
                        started_at=first_start,
                        ended_at=first_start + timedelta(minutes=30),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("10.0"),
                        start_odometer_miles=Decimal("1000.0"),
                        end_odometer_miles=Decimal("1010.0"),
                        source="auto",
                    ),
                    Trip(
                        trip_date=second_start.date(),
                        started_at=second_start,
                        ended_at=second_start + timedelta(minutes=30),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.0"),
                        start_odometer_miles=Decimal("3010.0"),
                        end_odometer_miles=Decimal("3015.0"),
                        source="auto",
                    ),
                ]
            )
            db.commit()

        response = client.post(
            "/diagnostics/odometer/emergency-rebuild",
            data={"odometer_miles": "2000.0"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "odometer_test=pass" in response.headers["location"]
        assert "discarded+1+odometer+gaps" in response.headers["location"]
        backup_files = list_automatic_backup_files(settings.automatic_backup_dir)
        assert len(backup_files) == 1
        assert backup_files[0].reason == "emergency"
        backup_payload = json.loads(gzip.decompress(backup_files[0].path.read_bytes()))
        assert backup_payload["tables"]["trips"][1]["start_odometer_miles"] == "3010.0"
        with session_factory() as db:
            trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
            checkpoint = db.scalar(select(TripProcessingCheckpoint))
            assert [trip.miles for trip in trips] == [Decimal("10.0"), Decimal("5.0")]
            assert trips[0].start_odometer_miles == Decimal("1985.0")
            assert trips[1].end_odometer_miles == Decimal("2000.0")
            assert checkpoint is not None
            assert checkpoint.odometer_anchor_miles == Decimal("2000.0")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_manual_odometer_at_home_aligns_all_trips_and_preserves_gaps() -> None:
    client, session_factory = _test_client_session()
    first_start = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    second_start = datetime(2026, 7, 14, 13, 0, tzinfo=UTC)
    try:
        with session_factory() as db:
            home = _site("Home")
            db.add_all(
                [
                    home,
                    Trip(
                        trip_date=first_start.date(),
                        started_at=first_start,
                        ended_at=first_start + timedelta(minutes=30),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("10.0"),
                        start_odometer_miles=Decimal("1000.0"),
                        end_odometer_miles=Decimal("1010.0"),
                        source="auto",
                    ),
                    Trip(
                        trip_date=second_start.date(),
                        started_at=second_start,
                        ended_at=second_start + timedelta(minutes=30),
                        start_latitude=Decimal("42.3314"),
                        start_longitude=Decimal("-83.0458"),
                        end_latitude=Decimal("42.3440"),
                        end_longitude=Decimal("-83.0600"),
                        miles=Decimal("5.0"),
                        start_odometer_miles=Decimal("1025.0"),
                        end_odometer_miles=Decimal("1030.0"),
                        source="auto",
                    ),
                ]
            )
            db.flush()
            db.add(
                OwnTracksLocation(
                    captured_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                    received_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                    latitude=home.latitude,
                    longitude=home.longitude,
                    raw_payload={"_type": "location", "inregions": ["Home"]},
                )
            )
            db.commit()

        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "2000.0"},
        )

        assert response.status_code == 200
        assert (
            "Manual odometer reading saved and trip odometers adjusted from Home."
            in response.text
        )
        with session_factory() as db:
            trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
            checkpoint = db.scalar(select(TripProcessingCheckpoint))
            assert trips[0].start_odometer_miles == Decimal("1970.0")
            assert trips[0].end_odometer_miles == Decimal("1980.0")
            assert trips[1].start_odometer_miles == Decimal("1995.0")
            assert trips[1].end_odometer_miles == Decimal("2000.0")
            assert trips[1].start_odometer_miles - trips[0].end_odometer_miles == Decimal("15.0")
            assert trips[1].end_odometer_source == "manual"
            assert checkpoint is not None
            assert checkpoint.odometer_anchor_miles == Decimal("2000.0")
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_shows_app_degraded_banner(monkeypatch) -> None:
    degraded_snapshot = AppHealthSnapshot(
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
        checked_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "trip_tracker.web.routes.build_app_health_snapshot",
        lambda **_kwargs: degraded_snapshot,
    )
    client, _session_factory = _test_client_session()
    try:
        response = client.get("/diagnostics")

        assert response.status_code == 200
        assert 'class="app-health-banner warning"' in response.text
        assert "App Degraded" in response.text
        assert "1 monitored issue detected." in response.text
        assert "Database latency elevated" in response.text
        assert "Database round trip is 750.0 ms." in response.text
        assert response.text.index("App Degraded") < response.text.index("<h2>Application</h2>")
    finally:
        app.dependency_overrides.clear()


def test_database_latency_indicator_uses_health_thresholds() -> None:
    settings = Settings(
        database_url="sqlite://",
        app_health_db_latency_warning_ms=250,
        app_health_db_latency_critical_ms=750,
    )

    assert _database_latency_indicator_class(settings, 12.5) == "good"
    assert _database_latency_indicator_class(settings, 250) == "warning"
    assert _database_latency_indicator_class(settings, None) == "warning"
    assert _database_latency_indicator_class(settings, 750) == "bad"


def test_diagnostics_manual_odometer_card_shows_current_odometer() -> None:
    now = datetime(2026, 6, 20, 13, 30, tzinfo=UTC)
    rendered = templates.env.get_template("diagnostics.html").render(
        {
            "settings": Settings(database_url="sqlite://"),
            "app_version": __version__,
            "database_url": "sqlite://",
            "runtime_status": _runtime_status(),
            "database_stats": SimpleNamespace(
                latency_ms=1.2,
                latency_display="1.2 ms",
                latency_indicator_class="good",
                size_display="128.0 KB",
                total_records_display="42 records",
                pool_display="0 in use / 5 pool, 0 overflow",
                timeout_display="10s connect, 30s pool",
            ),
            "app_health_snapshot": AppHealthSnapshot(
                status="ok",
                severity="ok",
                issues=(),
                checked_at=now,
            ),
            "location_count": 1_234,
            "site_count": 10_000,
            "trip_count": 100_000,
            "gas_snapshot_count": 1_000_000,
            "gas_price_extremes": SimpleNamespace(
                lowest_display="None",
                current_display="None",
                monthly_average_display="None",
                highest_display="None",
            ),
            "latest_location": None,
            "last_owntracks_received_at": None,
            "last_owntracks_received_age": "Never",
            "latest_snapshot": None,
            "latest_monthly_gas": None,
            "latest_odometer": {
                "value": Decimal("43210.4"),
                "source": "owntracks_estimate",
                "position": "Rolling",
                "recorded_at": now,
            },
            "disk_usages": [
                SimpleNamespace(
                    primary_path="/data/logs",
                    paths=("/data/logs",),
                    total_display="100.0 GB",
                    used_display="62.0 GB",
                    free_display="38.0 GB",
                    used_percent_display="62.0%",
                    used_percent_style="62.0%",
                )
            ],
            "database_summary": SimpleNamespace(
                size_display="128.0 KB",
                total_records_display="42 records",
            ),
            "recent_locations": [],
            "owntracks_entries_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "movement_state": SimpleNamespace(
                state="none",
                label="No data",
                site_name=None,
                arrived_at=None,
                detected_at=None,
                distance_miles=None,
            ),
            "inside_home": False,
            "movement_state_changes": [],
            "movement_state_changes_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "login_success_entries": [],
            "login_success_entries_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "login_failure_entries": [],
            "login_failure_entries_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "login_failure_ip_statuses": {},
            "cloudflare_ip_blocks": [],
            "cloudflare_ip_blocks_page": SimpleNamespace(
                first_item=0,
                last_item=0,
                total=0,
                has_previous=False,
                has_next=False,
                page=1,
                total_pages=1,
            ),
            "cloudflare_ip_blocking_configured": False,
            "passkeys": [],
            "passkey_origin": "",
            "passkey_rp_id": "",
            "cloudflare_block_result": None,
            "manual_odometer_result": None,
            "eia_test_result": None,
            "restore_result": None,
            "backup_restore_enabled": False,
            "automatic_backups_enabled": False,
            "automatic_backup_dir": "/tmp/trip-tracker-backups",
            "automatic_backups": [],
            "backup_upload_max_mb": 10,
        }
    )

    manual_card_start = rendered.index("<h2>Manual Odometer</h2>")
    manual_card_end = rendered.index("<h2>EIA API</h2>")
    manual_card = rendered[manual_card_start:manual_card_end]
    assert "Current Odometer" in manual_card
    assert "43,210.4 miles" in manual_card
    assert "OwnTracks estimate" in manual_card
    assert "Rolling" in manual_card
    assert 'formaction="/diagnostics/odometer/emergency-rebuild"' in manual_card
    assert "Emergency Rebuild" in manual_card
    assert "Available only while inside the Home waypoint" in manual_card

    api_tests_start = rendered.index('<section id="api-tests" class="diagnostics-grid">')
    app_card_start = rendered.index("<h2>Application</h2>")
    status_card_start = rendered.index("<h2>System Status</h2>")
    data_card_start = rendered.index("<h2>Data</h2>")
    latest_records_start = rendered.index("<h2>Latest Records</h2>")
    owntracks_card_start = rendered.index('<div id="owntracks-current-state" class="panel">')
    eia_card_start = rendered.index("<h2>EIA API</h2>")
    passkey_card_start = rendered.index('<div id="passkeys" class="panel">')
    hard_drive_start = rendered.index("<h2>Hard Drive Space</h2>")
    state_log_start = rendered.index('<section id="owntracks-state-log" class="panel">')
    api_tests_section = rendered[api_tests_start:state_log_start]
    assert (
        api_tests_start
        < app_card_start
        < status_card_start
        < data_card_start
        < latest_records_start
        < owntracks_card_start
        < manual_card_start
        < eia_card_start
        < passkey_card_start
        < hard_drive_start
        < state_log_start
    )
    assert manual_card_end == eia_card_start
    assert api_tests_section.count('class="panel"') == 9
    assert "Application" in api_tests_section
    assert "System Status" in api_tests_section
    status_card = rendered[status_card_start:data_card_start]
    assert "PostgreSQL Server" in status_card
    assert "Remote PostgreSQL - db.internal" in status_card
    assert "Latency" in status_card
    assert "1.2 ms" in status_card
    assert 'class="status-dot good"' in status_card
    assert "Database Size" in status_card
    assert "128.0 KB" in status_card
    assert "Total Records" in status_card
    assert "42 records" in status_card
    assert "Pool" in status_card
    assert "0 in use / 5 pool, 0 overflow" in status_card
    assert "Timeouts" in status_card
    assert "10s connect, 30s pool" in status_card
    assert "Primary Buffer" not in status_card
    assert "Backup Buffer" not in status_card
    assert "2 queued" not in status_card
    assert "3 queued" not in status_card
    assert "status-dot good" in status_card
    assert "Data" in api_tests_section
    data_card = rendered[data_card_start:latest_records_start]
    assert "1,234" in data_card
    assert "10,000" in data_card
    assert "100,000" in data_card
    assert "1,000,000" in data_card
    assert "Lowest Queried Gas Price" not in data_card
    assert "Highest Queried Gas Price" not in data_card
    assert "Lowest Gas Price" in data_card
    assert "Current Gas Price" in data_card
    assert "Monthly Average Gas Price" in data_card
    assert "Highest Gas Price" in data_card
    assert data_card.index("Lowest Gas Price") < data_card.index("Current Gas Price")
    assert data_card.index("Current Gas Price") < data_card.index("Monthly Average Gas Price")
    assert data_card.index("Monthly Average Gas Price") < data_card.index("Highest Gas Price")
    assert "Latest Records" in api_tests_section
    assert "OwnTracks State" in api_tests_section
    assert "Manual Odometer" in api_tests_section
    assert "EIA API" in api_tests_section
    assert "Configure Passkey" in api_tests_section
    assert "Hard Drive Space" in api_tests_section
    assert "Used space as a share of each drive" in rendered
    assert "drive-space-track" in rendered
    assert 'style="width: 62.0%"' in rendered
    disk_details_start = rendered.index('<dl class="diagnostic-list">', hard_drive_start)
    database_summary_start = rendered.index('<div class="database-summary">')
    assert hard_drive_start < disk_details_start < database_summary_start < state_log_start
    database_summary = rendered[database_summary_start:state_log_start]
    assert "Database Data" in database_summary
    assert "Database Size" in database_summary
    assert "128.0 KB" in database_summary
    assert "Total Records" in database_summary
    assert "42 records" in database_summary

    backup_start = rendered.index('<section id="data-backup" class="panel">')
    cloudflare_start = rendered.index('<section id="cloudflare-blocked-ips" class="panel">')
    assert cloudflare_start < backup_start
    assert "App Log" not in rendered
    assert "Full Data Backup" in rendered[backup_start:]


def test_diagnostics_compact_table_and_log_styles() -> None:
    stylesheet = Path("trip_tracker/web/static/styles.css").read_text(encoding="utf-8")

    assert ".automatic-backup-table .backup-file-name" in stylesheet
    assert ".dashboard-loading-shell" in stylesheet
    assert ".loading-spinner" in stylesheet
    assert ".trips-summary-grid" in stylesheet
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in stylesheet
    assert ".work-trip-counts" in stylesheet
    assert ".work-trip-counts > div" in stylesheet
    assert "grid-template-rows: 12px auto;" in stylesheet
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in stylesheet
    assert "font-size: 26px;" in stylesheet
    assert "grid-template-columns: 1fr;\n  }\n\n  .work-trip-counts dd" not in stylesheet
    assert (
        ".stats-grid {\n  display: grid;\n  grid-template-columns: repeat(6, minmax(0, 1fr));"
        in stylesheet
    )
    assert ".distance-card {\n  display: flex;\n  min-height: 96px;" in stylesheet
    assert ".distance-card strong" in stylesheet
    assert ".trip-summary-card strong" in stylesheet
    assert ".extra-expense-form" in stylesheet
    assert ".expense-reason-input" in stylesheet
    assert ".compact-status-line" in stylesheet
    assert ".waypoint-pagination" in stylesheet
    assert ".diagnostics-pagination" in stylesheet
    assert ".status-dot.good" in stylesheet
    assert ".status-dot.warning" in stylesheet
    assert ".status-dot.bad" in stylesheet
    assert ".panel-toolbar .pagination-controls" in stylesheet
    assert ".pagination-button-row .button-link" in stylesheet
    assert "flex: 1 1 0;" in stylesheet
    assert "text-overflow: ellipsis;" in stylesheet
    assert ".log-view" not in stylesheet


def test_diagnostics_disk_usage_combines_paths_on_same_drive(tmp_path) -> None:
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    other_dir = tmp_path / "other"
    backup_dir.mkdir(parents=True)
    other_dir.mkdir()

    def fake_disk_usage(path):
        if path in {data_dir, backup_dir}:
            return SimpleNamespace(total=1_000, used=600, free=400)
        return SimpleNamespace(total=2_000, used=500, free=1_500)

    disk_usages = _diagnostic_disk_usages(
        (str(data_dir), str(backup_dir), str(other_dir)),
        disk_usage_func=fake_disk_usage,
    )

    assert len(disk_usages) == 2
    combined_disk = next(item for item in disk_usages if item.total_bytes == 1_000)
    assert combined_disk.paths == (str(data_dir), str(backup_dir))
    assert combined_disk.used_bytes == 600
    assert combined_disk.free_bytes == 400
    assert combined_disk.used_percent_style == "60.0%"


def test_diagnostics_database_summary_counts_all_app_records() -> None:
    db = _session()
    _seed_full_backup_data(db)

    summary = _diagnostic_database_summary(db, "sqlite://")

    assert summary.total_records == 7
    assert summary.total_records_display == "7 records"
    assert summary.size_bytes is not None
    assert summary.size_bytes > 0
    assert summary.size_display.endswith(("B", "KB", "MB"))


def test_diagnostics_gas_price_summary_uses_snapshots_and_current_month_average() -> None:
    db = _session()

    empty_extremes = _diagnostic_gas_price_extremes(db)
    assert empty_extremes.lowest_price_per_gallon is None
    assert empty_extremes.current_price_per_gallon is None
    assert empty_extremes.monthly_average_price_per_gallon is None
    assert empty_extremes.highest_price_per_gallon is None
    assert empty_extremes.lowest_display == "None"
    assert empty_extremes.current_display == "None"
    assert empty_extremes.monthly_average_display == "None"
    assert empty_extremes.highest_display == "None"
    current_year, current_month = _current_year_month()

    db.add_all(
        [
            GasPriceSnapshot(
                observed_on=date(2026, 6, 1),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("4.299"),
                source="test",
                source_detail="high queried price",
            ),
            GasPriceSnapshot(
                observed_on=date(2026, 6, 2),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("2.999"),
                source="test",
                source_detail="low queried price",
            ),
            GasPriceSnapshot(
                observed_on=date(2026, 6, 3),
                state="MI",
                grade="regular",
                price_per_gallon=Decimal("3.499"),
                source="test",
                source_detail="latest queried price",
            ),
            MonthlyGasPrice(
                year=2026,
                month=6,
                state="MI",
                average_price_per_gallon=Decimal("9.999"),
                buffer_per_gallon=Decimal("0.500"),
                effective_rate=Decimal("10.499"),
                source="test",
                source_detail="monthly average must not affect extrema",
            ),
            MonthlyGasPrice(
                year=current_year,
                month=current_month,
                state="MI",
                average_price_per_gallon=Decimal("3.555"),
                buffer_per_gallon=Decimal("0.500"),
                effective_rate=Decimal("4.055"),
                source="test",
                source_detail="current month average",
            ),
        ]
    )

    extremes = _diagnostic_gas_price_extremes(db)

    assert extremes.lowest_price_per_gallon == Decimal("2.999")
    assert extremes.highest_price_per_gallon == Decimal("4.299")
    assert extremes.current_price_per_gallon == Decimal("3.499")
    assert extremes.monthly_average_price_per_gallon == Decimal("3.555")
    assert extremes.lowest_display == "$2.999"
    assert extremes.current_display == "$3.499"
    assert extremes.monthly_average_display == "$3.555"
    assert extremes.highest_display == "$4.299"


def test_diagnostics_manual_odometer_form_rejects_nonpositive_reading() -> None:
    client, session_factory = _test_client_session()
    try:
        response = client.post(
            "/diagnostics/odometer",
            data={"odometer_miles": "0"},
        )

        assert response.status_code == 200
        assert "Fail" in response.text
        assert "Odometer reading must be greater than zero." in response.text
        with session_factory() as db:
            assert db.scalar(select(TripProcessingCheckpoint)) is None
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_eia_api_test_button_reports_pass(monkeypatch) -> None:
    class FakeEiaProvider:
        def current_regular_price(self, state: str) -> GasPriceReading:
            return GasPriceReading(
                state=state,
                grade="regular",
                price_per_gallon=Decimal("3.250"),
                source="eia_series",
                source_detail="test",
                observed_on=datetime(2026, 6, 14, tzinfo=UTC).date(),
            )

    monkeypatch.setattr(
        "trip_tracker.web.routes.get_settings",
        lambda: Settings(
            database_url="sqlite://",
            eia_api_key="configured",
            eia_series_id="PET.EMM_EPMR_PTE_SMI_DPG.W",
        ),
    )
    monkeypatch.setattr("trip_tracker.web.routes.EiaSeriesProvider", FakeEiaProvider)
    client, _ = _test_client_session()
    try:
        response = client.post("/diagnostics/test/eia")

        assert response.status_code == 200
        assert "EIA API" in response.text
        assert "Pass" in response.text
        assert "$3.250" in response.text
    finally:
        app.dependency_overrides.clear()


def test_aaa_gas_provider_uses_local_observed_date(monkeypatch) -> None:
    class FakeResponse:
        text = "Current Avg.</td><td>$3.250</td>"

        def raise_for_status(self) -> None:
            return None

    now = datetime(2026, 6, 11, 21, 30, tzinfo=UTC)
    monkeypatch.setattr(
        "trip_tracker.services.gas_prices.httpx.get",
        lambda *_, **__: FakeResponse(),
    )
    monkeypatch.setattr("trip_tracker.services.gas_prices.local_today", lambda: now.date())

    reading = AaaMichiganGasPriceProvider().current_regular_price("MI")

    assert reading.observed_on == now.date()
