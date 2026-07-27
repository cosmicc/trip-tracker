import base64
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nacl.secret import SecretBox
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from trip_tracker.api.routes import get_owntracks_session_factory
from trip_tracker.app import app
from trip_tracker.config import Settings
from trip_tracker.database import get_db
from trip_tracker.models import Base, OwnTracksLocation


def _test_client_session() -> tuple[TestClient, sessionmaker[Session]]:
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
    app.dependency_overrides[get_owntracks_session_factory] = lambda: session_factory
    return TestClient(app), session_factory


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    for module_name in (
        "trip_tracker.api.deps",
        "trip_tracker.api.routes",
        "trip_tracker.app",
        "trip_tracker.services.mileage",
        "trip_tracker.services.owntracks",
        "trip_tracker.services.trip_processor",
        "trip_tracker.web.auth",
    ):
        monkeypatch.setattr(f"{module_name}.get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        "trip_tracker.api.routes.run_migrations_once_on_reconnect",
        lambda: None,
    )


def _owntracks_secretbox_key(secret: str) -> bytes:
    return secret.encode("utf-8").ljust(SecretBox.KEY_SIZE, b"\0")


def _encrypted_owntracks_payload(payload: dict, secret: str) -> dict:
    encrypted = SecretBox(_owntracks_secretbox_key(secret)).encrypt(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        nonce=b"\0" * SecretBox.NONCE_SIZE,
    )
    return {
        "_type": "encrypted",
        "data": base64.b64encode(bytes(encrypted)).decode("ascii"),
    }


def test_owntracks_endpoint_requires_basic_auth_and_encrypted_payload(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, session_factory = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        unauthenticated = client.post("/api/owntracks", json=encrypted_payload)
        plaintext = client.post(
            "/api/owntracks",
            json=payload,
            auth=("owntracks", "owntracks-password"),
        )
        accepted = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert unauthenticated.status_code == 401
        assert plaintext.status_code == 400
        assert accepted.status_code == 200
        with session_factory() as db:
            location = db.scalar(select(OwnTracksLocation))
            assert location is not None
            assert location.latitude == Decimal("42.3314")
            assert location.raw_payload["_decrypted"] is True
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_fails_closed_without_encryption_key(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
    }
    try:
        response = client.post(
            "/api/owntracks",
            json=payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "OWNTRACKS_ENCRYPTION_KEY is not configured"}
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_uses_dedicated_database_session(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, session_factory = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        response = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "X-Trip-Tracker-OwnTracks-Buffered" not in response.headers
        with session_factory() as db:
            assert db.scalar(select(func.count(OwnTracksLocation.id))) == 1
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_returns_retryable_503_when_database_is_offline(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)

    def offline_session_factory():
        raise OperationalError("INSERT", {}, Exception("database offline"))

    app.dependency_overrides[get_owntracks_session_factory] = lambda: offline_session_factory
    client = TestClient(app)
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        response = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Database is unavailable; OwnTracks should retry later"
        }
        assert response.headers["Retry-After"] == "30"
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        app.dependency_overrides.clear()


def test_owntracks_endpoint_waits_for_migrations_before_accepting(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)

    def migrations_not_ready() -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        "trip_tracker.api.routes.run_migrations_once_on_reconnect",
        migrations_not_ready,
    )
    client, session_factory = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        response = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Database is not ready; OwnTracks should retry later"
        }
        assert response.headers["Retry-After"] == "30"
        with session_factory() as db:
            assert db.scalar(select(func.count(OwnTracksLocation.id))) == 0
    finally:
        app.dependency_overrides.clear()


def test_owntracks_exact_http_retry_is_not_stored_twice(monkeypatch) -> None:
    settings = Settings(
        database_url="sqlite://",
        owntracks_username="owntracks",
        owntracks_password="owntracks-password",
        owntracks_encryption_key="owntracks-secret",
        automatic_trip_processing_enabled=False,
    )
    _patch_settings(monkeypatch, settings)
    client, session_factory = _test_client_session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(datetime(2026, 6, 30, 12, 0, tzinfo=UTC).timestamp()),
        "tid": "IP",
        "topic": "owntracks/ian/phone",
    }
    encrypted_payload = _encrypted_owntracks_payload(payload, settings.owntracks_encryption_key)
    try:
        first = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )
        retry = client.post(
            "/api/owntracks",
            json=encrypted_payload,
            auth=("owntracks", "owntracks-password"),
        )

        assert first.status_code == 200
        assert retry.status_code == 200
        with session_factory() as db:
            assert db.scalar(select(func.count(OwnTracksLocation.id))) == 1
    finally:
        app.dependency_overrides.clear()


def test_non_owntracks_api_requires_separate_bearer_key(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://", web_api_key="web-api-secret")
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    try:
        health = client.get("/api/health")
        missing = client.get("/api/locations")
        wrong = client.get("/api/locations", headers={"Authorization": "Bearer wrong"})
        accepted = client.get("/api/locations", headers={"Authorization": "Bearer web-api-secret"})

        assert health.status_code == 200
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert accepted.status_code == 200
        assert accepted.json() == []
    finally:
        app.dependency_overrides.clear()


def test_non_owntracks_api_fails_closed_when_web_api_key_is_missing(monkeypatch) -> None:
    settings = Settings(database_url="sqlite://", web_api_key="")
    _patch_settings(monkeypatch, settings)
    client, _ = _test_client_session()
    try:
        response = client.get("/api/locations")

        assert response.status_code == 503
        assert response.json() == {"detail": "WEB_API_KEY is not configured"}
    finally:
        app.dependency_overrides.clear()


def test_api_secret_settings_are_validated() -> None:
    with pytest.raises(ValidationError, match="OWNTRACKS_ENCRYPTION_KEY"):
        Settings(owntracks_encryption_key="x" * 33)

    with pytest.raises(ValidationError, match="OWNTRACKS_USERNAME and OWNTRACKS_PASSWORD"):
        Settings(
            owntracks_username="",
            owntracks_password="",
            owntracks_encryption_key="owntracks-secret",
        )

    with pytest.raises(ValidationError, match="WEB_API_KEY"):
        Settings(
            app_env="production",
            secret_key="production-test-secret",
            web_login_username="admin",
            web_login_password="secret-password",
            owntracks_username="owntracks",
            owntracks_password="owntracks-password",
            owntracks_encryption_key="owntracks-secret",
            web_api_key="",
        )
