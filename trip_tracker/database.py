from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from trip_tracker.config import Settings, get_settings
from trip_tracker.database_engine import database_engine_options, normalized_database_url

settings = get_settings()


class DatabaseConfigurationError(RuntimeError):
    """Raised when database configuration prevents SQLAlchemy engine creation."""


class UnavailableDatabaseEngine:
    """Engine-like object that raises a classified database-unavailable error."""

    dialect = SimpleNamespace(name="invalid")

    def __init__(self, message: str, original_error: BaseException) -> None:
        self.message = message
        self.original_error = original_error

    def connect(self, *_args: Any, **_kwargs: Any) -> Any:
        """Raise the stored configuration error instead of opening a connection."""

        raise DatabaseConfigurationError(self.message) from self.original_error

    def begin(self, *_args: Any, **_kwargs: Any) -> Any:
        """Raise the stored configuration error instead of opening a transaction."""

        raise DatabaseConfigurationError(self.message) from self.original_error

    def dispose(self) -> None:
        """Match the SQLAlchemy Engine dispose API for shutdown/test cleanup paths."""

    def __getattr__(self, _name: str) -> Any:
        raise DatabaseConfigurationError(self.message) from self.original_error


def create_configured_engine(application_settings: Settings) -> Any:
    """Create the configured SQLAlchemy engine or an unavailable-engine placeholder."""

    try:
        database_url = normalized_database_url(application_settings.database_url)
        engine_options = database_engine_options(application_settings)
        return create_engine(database_url, **engine_options)
    except (ArgumentError, ModuleNotFoundError) as exc:
        return UnavailableDatabaseEngine(
            f"Invalid DATABASE_URL configuration: {exc}",
            exc,
        )


engine = create_configured_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def is_database_unavailable_error(exc: BaseException) -> bool:
    """Return whether an exception represents an unavailable database connection."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, BaseExceptionGroup):
            return any(is_database_unavailable_error(child) for child in current.exceptions)
        if isinstance(
            current,
            (DatabaseConfigurationError, OperationalError, SQLAlchemyTimeoutError),
        ):
            return True
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        current = current.__cause__ if isinstance(current.__cause__, BaseException) else None
    return False


def database_is_reachable() -> bool:
    """Check whether the configured database accepts a simple query."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        if is_database_unavailable_error(exc):
            return False
        raise
    return True


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
