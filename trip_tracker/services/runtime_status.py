from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from trip_tracker.config import Settings

LOCAL_POSTGRES_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}


@dataclass(frozen=True)
class RuntimeDatabaseStatus:
    """Database availability and configured endpoint placement for status cards."""

    available: bool
    engine_label: str
    placement_label: str
    host_label: str

    @property
    def indicator_class(self) -> str:
        """Return the CSS state class for the status indicator dot."""

        return "good" if self.available else "bad"

    @property
    def state_label(self) -> str:
        """Return a short database state label."""

        return "Reachable" if self.available else "Unavailable"

    @property
    def detail_label(self) -> str:
        """Return a compact endpoint summary."""

        if self.host_label:
            return f"{self.placement_label} - {self.host_label}"
        return self.placement_label


@dataclass(frozen=True)
class RuntimeStatus:
    """Database status for web diagnostics and outage handling."""

    database: RuntimeDatabaseStatus


def build_runtime_status(
    settings: Settings,
    *,
    database_available: bool,
) -> RuntimeStatus:
    """Build a database status snapshot without querying PostgreSQL."""

    return RuntimeStatus(database=_database_status(settings, available=database_available))


def _database_status(settings: Settings, *, available: bool) -> RuntimeDatabaseStatus:
    """Describe the configured database endpoint and whether it is reachable."""

    try:
        parsed_url = make_url(settings.database_url)
    except ArgumentError:
        return RuntimeDatabaseStatus(
            available=available,
            engine_label="Database",
            placement_label="Invalid URL",
            host_label="",
        )
    backend_name = parsed_url.get_backend_name()
    if backend_name != "postgresql":
        return RuntimeDatabaseStatus(
            available=available,
            engine_label=backend_name.upper(),
            placement_label="Local test database" if backend_name == "sqlite" else "Configured",
            host_label=str(parsed_url.database or ""),
        )

    host = (parsed_url.host or "").strip()
    normalized_host = host.casefold()
    placement = "Local/Bundled PostgreSQL"
    if normalized_host not in LOCAL_POSTGRES_HOSTS:
        placement = "Remote PostgreSQL"
    return RuntimeDatabaseStatus(
        available=available,
        engine_label="PostgreSQL",
        placement_label=placement,
        host_label=host or "local socket",
    )
