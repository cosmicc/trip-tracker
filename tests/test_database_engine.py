from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from trip_tracker.config import Settings
from trip_tracker.database import (
    DatabaseConfigurationError,
    UnavailableDatabaseEngine,
    create_configured_engine,
    is_database_unavailable_error,
)
from trip_tracker.database_engine import database_engine_options, normalized_database_url


def test_postgresql_engine_options_are_configurable_for_network_database() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://triptracker:secret@db-server:5432/trip_tracker",
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout_seconds=12,
        database_pool_recycle_seconds=900,
        database_connect_timeout_seconds=7,
    )

    options = database_engine_options(settings)

    assert options["pool_pre_ping"] is True
    assert options["pool_size"] == 3
    assert options["max_overflow"] == 4
    assert options["pool_timeout"] == 12
    assert options["pool_recycle"] == 900
    assert options["pool_use_lifo"] is True
    assert options["connect_args"] == {"connect_timeout": 7}


def test_bare_postgresql_url_uses_installed_psycopg_driver() -> None:
    settings = Settings(
        database_url="postgresql://mileage:secret@db-server:5432/trip_tracker",
    )

    engine = create_configured_engine(settings)

    assert engine.url.drivername == "postgresql+psycopg"
    assert normalized_database_url(settings.database_url).startswith("postgresql+psycopg://")


def test_sqlite_engine_options_skip_postgresql_pool_arguments() -> None:
    settings = Settings(
        database_url="sqlite://",
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout_seconds=12,
        database_pool_recycle_seconds=900,
        database_connect_timeout_seconds=7,
    )

    assert database_engine_options(settings) == {"pool_pre_ping": True}


def test_invalid_database_url_creates_unavailable_engine() -> None:
    settings = Settings(database_url="not a url")

    engine = create_configured_engine(settings)

    assert isinstance(engine, UnavailableDatabaseEngine)
    try:
        engine.connect()
    except DatabaseConfigurationError as exc:
        assert is_database_unavailable_error(exc)
        assert "Invalid DATABASE_URL configuration" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Invalid DATABASE_URL should not create a live engine")


def test_invalid_database_url_session_error_is_classified_unavailable() -> None:
    settings = Settings(database_url="not a url")
    engine = create_configured_engine(settings)
    session_factory = sessionmaker(bind=engine)

    try:
        with session_factory() as db:
            db.execute(text("SELECT 1"))
    except DatabaseConfigurationError as exc:
        assert is_database_unavailable_error(exc)
    else:  # pragma: no cover
        raise AssertionError("Invalid DATABASE_URL session should fail as unavailable")
