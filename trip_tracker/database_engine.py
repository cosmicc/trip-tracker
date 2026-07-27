from typing import Any

from sqlalchemy.engine import make_url

from trip_tracker.config import Settings


def normalized_database_url(database_url: str) -> str:
    """Return a SQLAlchemy URL that uses the installed PostgreSQL driver."""

    parsed_url = make_url(database_url)
    if parsed_url.drivername == "postgresql":
        return parsed_url.set(drivername="postgresql+psycopg").render_as_string(
            hide_password=False
        )
    return database_url


def database_engine_options(settings: Settings) -> dict[str, Any]:
    """Return SQLAlchemy engine options for the configured database backend."""

    options: dict[str, Any] = {"pool_pre_ping": True}
    backend_name = make_url(normalized_database_url(settings.database_url)).get_backend_name()
    if backend_name != "postgresql":
        return options

    options.update(
        {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_timeout": settings.database_pool_timeout_seconds,
            "pool_recycle": settings.database_pool_recycle_seconds,
            "pool_use_lifo": True,
            "connect_args": {
                "connect_timeout": settings.database_connect_timeout_seconds,
            },
        }
    )
    return options
