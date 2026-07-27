"""Portable full-data backup and restore helpers for Trip Tracker."""

import asyncio
import gzip
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Column, Table
from sqlalchemy.sql.sqltypes import Date, DateTime, Numeric

from trip_tracker import database
from trip_tracker.models import Base, Site
from trip_tracker.services.timezone import datetime_to_local
from trip_tracker.services.waypoints import owntracks_waypoints_export

if TYPE_CHECKING:
    from trip_tracker.config import Settings

logger = logging.getLogger(__name__)

BACKUP_FORMAT = "trip_tracker.full_backup"
LEGACY_BACKUP_FORMATS = frozenset({"mileage_logger.full_backup"})
BACKUP_VERSION = 1
BACKUP_MEDIA_TYPE = "application/gzip"
BACKUP_UPLOAD_MAX_BYTES = 250 * 1024 * 1024
BACKUP_TABLES = tuple(Base.metadata.sorted_tables)
BACKUP_TABLE_NAMES = tuple(table.name for table in BACKUP_TABLES)
AUTOMATIC_BACKUP_FILENAME_PREFIX = "trip-tracker-auto-backup-"
AUTOMATIC_BACKUP_STARTUP_FILENAME_PREFIX = "trip-tracker-auto-backup-startup-"
AUTOMATIC_BACKUP_EMERGENCY_FILENAME_PREFIX = "trip-tracker-auto-backup-emergency-"
LEGACY_AUTOMATIC_BACKUP_FILENAME_PREFIX = "mileage-logger-auto-backup-"
LEGACY_AUTOMATIC_BACKUP_STARTUP_FILENAME_PREFIX = (
    "mileage-logger-auto-backup-startup-"
)
LEGACY_AUTOMATIC_BACKUP_EMERGENCY_FILENAME_PREFIX = (
    "mileage-logger-auto-backup-emergency-"
)
AUTOMATIC_BACKUP_FILENAME_SUFFIX = ".json.gz"
AUTOMATIC_BACKUP_INTERVAL_SECONDS = 6 * 60 * 60
AUTOMATIC_RECENT_BACKUPS_TO_KEEP = 4
AUTOMATIC_PRIOR_DAILY_BACKUP_DAYS_TO_KEEP = 2
AUTOMATIC_BACKUP_REASONS = {"emergency", "scheduled", "startup"}
_BACKUP_RESTORE_LOCK = threading.RLock()


class BackupValidationError(ValueError):
    """Raised when an uploaded backup is not a valid Trip Tracker backup."""


@dataclass(frozen=True)
class FullBackup:
    """Downloadable backup content and the row counts it contains."""

    filename: str
    content: bytes
    table_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


@dataclass(frozen=True)
class RestoreSummary:
    """Summary of rows restored from a validated backup."""

    table_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


@dataclass(frozen=True)
class AutomaticBackupFile:
    """One retained automatic backup file available for Diagnostics restore."""

    filename: str
    path: Path
    created_at_utc: datetime
    size_bytes: int
    reason: str = "scheduled"


@dataclass(frozen=True)
class AutomaticBackupResult:
    """Result of creating one automatic backup and pruning expired files."""

    backup_file: AutomaticBackupFile
    deleted_filenames: tuple[str, ...]


def full_backup_filename(now: datetime | None = None) -> str:
    """Return a timestamped backup filename safe for browser downloads."""

    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    timestamp = current_dt.strftime("%Y%m%d-%H%M%SZ")
    return f"trip-tracker-full-backup-{timestamp}.json.gz"


def automatic_backup_filename(
    now: datetime | None = None,
    *,
    reason: str = "scheduled",
) -> str:
    """Return a timestamped automatic backup filename safe for filesystem use."""

    if reason not in AUTOMATIC_BACKUP_REASONS:
        raise ValueError("Automatic backup reason must be emergency, scheduled, or startup.")
    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    timestamp = current_dt.strftime("%Y%m%d-%H%M%SZ")
    if reason == "startup":
        prefix = AUTOMATIC_BACKUP_STARTUP_FILENAME_PREFIX
    elif reason == "emergency":
        prefix = AUTOMATIC_BACKUP_EMERGENCY_FILENAME_PREFIX
    else:
        prefix = AUTOMATIC_BACKUP_FILENAME_PREFIX
    return f"{prefix}{timestamp}{AUTOMATIC_BACKUP_FILENAME_SUFFIX}"


def create_full_backup(db: Session, *, now: datetime | None = None) -> FullBackup:
    """Create a gzip-compressed JSON snapshot of every application data table."""

    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    table_payloads: dict[str, list[dict[str, Any]]] = {}
    table_counts: dict[str, int] = {}

    with _BACKUP_RESTORE_LOCK:
        for table in BACKUP_TABLES:
            rows = [
                _serialize_row(table, row)
                for row in db.execute(_select_table(table)).mappings()
            ]
            table_payloads[table.name] = rows
            table_counts[table.name] = len(rows)

        sites = list(db.scalars(select(Site).order_by(Site.name.asc(), Site.id.asc())))

    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": current_dt.isoformat(),
        "tables": table_payloads,
        "table_counts": table_counts,
        "schema": {
            table.name: [column.name for column in table.columns] for table in BACKUP_TABLES
        },
        "owntracks_waypoints": owntracks_waypoints_export(sites),
    }
    raw_content = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return FullBackup(
        filename=full_backup_filename(current_dt),
        content=gzip.compress(raw_content, mtime=0),
        table_counts=table_counts,
    )


def list_automatic_backup_files(backup_directory: str | Path) -> tuple[AutomaticBackupFile, ...]:
    """Return available automatic backups newest-first, ignoring unknown files."""

    directory = Path(backup_directory)
    try:
        entries = tuple(directory.iterdir())
    except FileNotFoundError:
        return ()
    except OSError:
        logger.exception("Could not list automatic backup directory path=%s", directory)
        return ()

    backup_files: list[AutomaticBackupFile] = []
    for entry in entries:
        backup_file = _automatic_backup_file_from_path(entry)
        if backup_file is not None:
            backup_files.append(backup_file)

    return tuple(sorted(backup_files, key=lambda item: item.created_at_utc, reverse=True))


def create_automatic_backup(
    db: Session,
    backup_directory: str | Path,
    *,
    now: datetime | None = None,
    reason: str = "scheduled",
) -> AutomaticBackupResult:
    """Write one full-data automatic backup file and purge expired backups."""

    if reason not in AUTOMATIC_BACKUP_REASONS:
        raise BackupValidationError("Automatic backup reason is not supported.")
    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    directory = _ensure_private_backup_directory(Path(backup_directory))
    backup = create_full_backup(db, now=current_dt)
    backup_path = directory / automatic_backup_filename(current_dt, reason=reason)
    _write_private_file_atomically(backup_path, backup.content)
    backup_file = _automatic_backup_file_from_path(backup_path)
    if backup_file is None:
        raise BackupValidationError("Automatic backup could not be verified after writing.")

    deleted_filenames = prune_automatic_backups(directory, now=current_dt)
    return AutomaticBackupResult(backup_file=backup_file, deleted_filenames=deleted_filenames)


def prune_automatic_backups(
    backup_directory: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Delete automatic backup files outside scheduled and daily retention windows."""

    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    backup_files = list_automatic_backup_files(backup_directory)
    retained_filenames = _retained_automatic_backup_filenames(backup_files, current_dt)
    deleted_filenames: list[str] = []

    for backup_file in backup_files:
        if backup_file.filename in retained_filenames:
            continue
        try:
            backup_file.path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("Could not delete expired automatic backup path=%s", backup_file.path)
            continue
        deleted_filenames.append(backup_file.filename)

    return tuple(deleted_filenames)


def read_automatic_backup_content(
    backup_directory: str | Path,
    filename: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read a named automatic backup after strict filename and size checks."""

    safe_filename = filename.strip()
    _validate_automatic_backup_filename(safe_filename)
    backup_path = Path(backup_directory) / safe_filename
    if backup_path.is_symlink():
        raise BackupValidationError("Automatic backup file is not a regular file.")
    try:
        stat_result = backup_path.stat()
    except FileNotFoundError as exc:
        raise BackupValidationError("Automatic backup file was not found.") from exc
    if not backup_path.is_file():
        raise BackupValidationError("Automatic backup file is not a regular file.")
    if stat_result.st_size > max_bytes:
        raise BackupValidationError("Automatic backup file is larger than the restore limit.")

    try:
        return backup_path.read_bytes()
    except OSError as exc:
        raise BackupValidationError("Automatic backup file could not be read.") from exc


def run_automatic_backup_once(
    application_settings: "Settings",
    *,
    reason: str = "scheduled",
) -> AutomaticBackupResult:
    """Create one scheduled backup using an isolated database session."""

    with database.SessionLocal() as db:
        result = create_automatic_backup(
            db,
            application_settings.automatic_backup_dir,
            reason=reason,
        )

    logger.info(
        "Created automatic Trip Tracker backup filename=%s reason=%s size_bytes=%s deleted=%s",
        result.backup_file.filename,
        result.backup_file.reason,
        result.backup_file.size_bytes,
        len(result.deleted_filenames),
    )
    return result


async def automatic_backup_scheduler(application_settings: "Settings") -> None:
    """Run automatic backups, retrying failed passes until storage recovers."""

    backup_reason = "startup"
    while True:
        next_attempt_delay = AUTOMATIC_BACKUP_INTERVAL_SECONDS
        backup_succeeded = False
        try:
            if not await asyncio.to_thread(database.database_is_reachable):
                logger.info(
                    "Automatic Trip Tracker backup paused because database is unavailable; "
                    "retrying in %s seconds",
                    application_settings.automatic_backup_retry_seconds,
                )
                next_attempt_delay = application_settings.automatic_backup_retry_seconds
            else:
                await asyncio.to_thread(
                    run_automatic_backup_once,
                    application_settings,
                    reason=backup_reason,
                )
                backup_succeeded = True
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            next_attempt_delay = application_settings.automatic_backup_retry_seconds
            logger.warning(
                "Automatic Trip Tracker backup storage is unavailable; retrying in %s "
                "seconds error=%s",
                next_attempt_delay,
                exc,
            )
        except Exception:
            next_attempt_delay = application_settings.automatic_backup_retry_seconds
            logger.exception(
                "Automatic Trip Tracker backup failed; retrying in %s seconds",
                next_attempt_delay,
            )

        if backup_succeeded:
            backup_reason = "scheduled"
        await asyncio.sleep(next_attempt_delay)


def restore_full_backup(db: Session, content: bytes) -> RestoreSummary:
    """Replace all application table rows with data from a validated backup."""

    rows_by_table = _validated_table_rows(_load_backup_payload(content))
    table_counts = {table.name: len(rows_by_table[table.name]) for table in BACKUP_TABLES}

    with _BACKUP_RESTORE_LOCK:
        try:
            _lock_postgresql_tables(db)
            for table in reversed(BACKUP_TABLES):
                db.execute(table.delete().execution_options(synchronize_session=False))
            for table in BACKUP_TABLES:
                rows = rows_by_table[table.name]
                if rows:
                    db.execute(insert(table), rows)
            _reset_postgresql_sequences(db)
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.warning(
        "Restored full database backup tables=%s total_rows=%s",
        len(table_counts),
        sum(table_counts.values()),
    )
    return RestoreSummary(table_counts=table_counts)


def _automatic_backup_file_from_path(path: Path) -> AutomaticBackupFile | None:
    """Return metadata for a valid automatic backup path or None for non-matches."""

    if path.is_symlink() or not path.is_file():
        return None
    parsed_filename = _parse_automatic_backup_filename(path.name)
    if parsed_filename is None:
        return None
    created_at_utc, reason = parsed_filename
    try:
        stat_result = path.stat()
    except OSError:
        logger.exception("Could not stat automatic backup path=%s", path)
        return None

    return AutomaticBackupFile(
        filename=path.name,
        path=path,
        created_at_utc=created_at_utc,
        size_bytes=stat_result.st_size,
        reason=reason,
    )


def _parse_automatic_backup_filename(filename: str) -> tuple[datetime, str] | None:
    """Parse current or pre-1.5 UTC backup filenames without weakening path checks."""

    if not filename.endswith(AUTOMATIC_BACKUP_FILENAME_SUFFIX):
        return None
    prefix_and_reason = next(
        (
            (prefix, reason)
            for prefix, reason in (
                (AUTOMATIC_BACKUP_STARTUP_FILENAME_PREFIX, "startup"),
                (AUTOMATIC_BACKUP_EMERGENCY_FILENAME_PREFIX, "emergency"),
                (AUTOMATIC_BACKUP_FILENAME_PREFIX, "scheduled"),
                (LEGACY_AUTOMATIC_BACKUP_STARTUP_FILENAME_PREFIX, "startup"),
                (LEGACY_AUTOMATIC_BACKUP_EMERGENCY_FILENAME_PREFIX, "emergency"),
                (LEGACY_AUTOMATIC_BACKUP_FILENAME_PREFIX, "scheduled"),
            )
            if filename.startswith(prefix)
        ),
        None,
    )
    if prefix_and_reason is None:
        return None
    prefix, reason = prefix_and_reason

    timestamp_text = filename[
        len(prefix) : -len(AUTOMATIC_BACKUP_FILENAME_SUFFIX)
    ]
    try:
        return datetime.strptime(timestamp_text, "%Y%m%d-%H%M%SZ").replace(tzinfo=UTC), reason
    except ValueError:
        return None


def _parse_automatic_backup_datetime(filename: str) -> datetime | None:
    """Parse a UTC timestamp from a legacy or reasoned automatic backup filename."""

    parsed_filename = _parse_automatic_backup_filename(filename)
    if parsed_filename is None:
        return None
    return parsed_filename[0]


def _validate_automatic_backup_filename(filename: str) -> None:
    """Reject traversal, hidden paths, and non-automatic backup filenames."""

    if not filename or Path(filename).name != filename:
        raise BackupValidationError("Automatic backup filename is not valid.")
    if _parse_automatic_backup_datetime(filename) is None:
        raise BackupValidationError("Automatic backup filename is not valid.")


def _ensure_private_backup_directory(backup_directory: Path) -> Path:
    """Create the automatic backup directory with owner-only permissions when possible."""

    backup_directory.mkdir(parents=True, exist_ok=True)
    try:
        backup_directory.chmod(0o700)
    except OSError:
        logger.warning(
            "Could not set automatic backup directory permissions path=%s",
            backup_directory,
        )
    return backup_directory


def _write_private_file_atomically(destination: Path, content: bytes) -> None:
    """Write backup bytes through a 0600 temporary file before atomic replace."""

    temporary_path = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            file_descriptor = None
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            logger.warning("Could not set automatic backup file permissions path=%s", destination)
    except FileExistsError as exc:
        raise BackupValidationError("Temporary automatic backup file already exists.") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary automatic backup path=%s", temporary_path)


def _retained_automatic_backup_filenames(
    backup_files: tuple[AutomaticBackupFile, ...],
    current_dt: datetime,
) -> set[str]:
    """Return filenames protected by scheduled and prior-day daily retention rules."""

    retained_filenames = {
        backup_file.filename
        for backup_file in backup_files[:AUTOMATIC_RECENT_BACKUPS_TO_KEEP]
    }
    current_local_date = datetime_to_local(current_dt).date()
    retained_daily_dates = {
        current_local_date - timedelta(days=day_offset)
        for day_offset in range(1, AUTOMATIC_PRIOR_DAILY_BACKUP_DAYS_TO_KEEP + 1)
    }
    daily_kept_dates: set[date] = set()
    for backup_file in backup_files:
        backup_local_date = datetime_to_local(backup_file.created_at_utc).date()
        if backup_local_date not in retained_daily_dates or backup_local_date in daily_kept_dates:
            continue
        retained_filenames.add(backup_file.filename)
        daily_kept_dates.add(backup_local_date)

    return retained_filenames


def _select_table(table: Table):
    """Build a deterministic select statement for one table."""

    statement = select(table)
    primary_key_columns = list(table.primary_key.columns)
    if primary_key_columns:
        statement = statement.order_by(*(column.asc() for column in primary_key_columns))
    return statement


def _serialize_row(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    return {
        column.name: _serialize_value(column, row[column.name])
        for column in table.columns
    }


def _serialize_value(column: Column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if not isinstance(value, datetime):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} expected datetime"
            )
        return value.isoformat()
    if isinstance(column.type, Date):
        if not isinstance(value, date):
            raise BackupValidationError(f"Column {column.table.name}.{column.name} expected date")
        return value.isoformat()
    if isinstance(column.type, Numeric):
        return str(value)
    return value


def _load_backup_payload(content: bytes) -> dict[str, Any]:
    if not content:
        raise BackupValidationError("Backup file is empty.")
    try:
        raw_content = gzip.decompress(content)
    except OSError:
        raw_content = content

    try:
        payload = json.loads(raw_content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BackupValidationError("Backup file is not valid UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise BackupValidationError("Backup file is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise BackupValidationError("Backup root must be a JSON object.")
    supported_formats = LEGACY_BACKUP_FORMATS | {BACKUP_FORMAT}
    if payload.get("format") not in supported_formats:
        raise BackupValidationError("Backup format is not supported.")
    if payload.get("version") != BACKUP_VERSION:
        raise BackupValidationError("Backup version is not supported.")
    return payload


def _validated_table_rows(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables_payload = payload.get("tables")
    if not isinstance(tables_payload, dict):
        raise BackupValidationError("Backup does not contain a tables object.")

    missing_tables = set(BACKUP_TABLE_NAMES) - set(tables_payload)
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise BackupValidationError(f"Backup is missing required tables: {missing}.")

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table in BACKUP_TABLES:
        raw_rows = tables_payload.get(table.name)
        if not isinstance(raw_rows, list):
            raise BackupValidationError(f"Backup table {table.name} must be a list.")
        rows_by_table[table.name] = [
            _validated_row(table, row, index) for index, row in enumerate(raw_rows, start=1)
        ]
    return rows_by_table


def _validated_row(table: Table, row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise BackupValidationError(f"Backup table {table.name} row {index} must be an object.")

    expected_columns = {column.name for column in table.columns}
    actual_columns = set(row)
    unexpected_columns = actual_columns - expected_columns
    if unexpected_columns:
        columns = ", ".join(sorted(unexpected_columns))
        raise BackupValidationError(
            f"Backup table {table.name} row {index} has unexpected columns: {columns}."
        )
    missing_columns = expected_columns - actual_columns
    if missing_columns:
        columns = ", ".join(sorted(missing_columns))
        raise BackupValidationError(
            f"Backup table {table.name} row {index} is missing columns: {columns}."
        )

    return {
        column.name: _deserialize_value(column, row[column.name])
        for column in table.columns
    }


def _deserialize_value(column: Column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if not isinstance(value, str):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} must be an ISO datetime string."
            )
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid datetime."
            ) from exc
    if isinstance(column.type, Date):
        if not isinstance(value, str):
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} must be an ISO date string."
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid date."
            ) from exc
    if isinstance(column.type, Numeric):
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise BackupValidationError(
                f"Column {column.table.name}.{column.name} has an invalid decimal value."
            ) from exc
    return value


def _lock_postgresql_tables(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return

    preparer = bind.dialect.identifier_preparer
    table_names = ", ".join(_quoted_table_name(table, preparer) for table in BACKUP_TABLES)
    db.execute(text(f"LOCK TABLE {table_names} IN ACCESS EXCLUSIVE MODE"))


def _quoted_table_name(table: Table, preparer) -> str:
    if table.schema:
        return f"{preparer.quote_schema(table.schema)}.{preparer.quote(table.name)}"
    return preparer.quote(table.name)


def _reset_postgresql_sequences(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in BACKUP_TABLES:
        integer_primary_keys = [
            column
            for column in table.primary_key.columns
            if column.autoincrement and column.type.python_type is int
        ]
        if len(integer_primary_keys) != 1:
            continue

        column = integer_primary_keys[0]
        sequence_name = db.scalar(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {
                "table_name": table.name,
                "column_name": column.name,
            },
        )
        if not sequence_name:
            continue

        max_value = db.scalar(select(column).order_by(column.desc()).limit(1))
        if max_value is None:
            db.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), 1, false)"),
                {"sequence_name": sequence_name},
            )
        else:
            db.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), :value, true)"),
                {"sequence_name": sequence_name, "value": int(max_value)},
            )
