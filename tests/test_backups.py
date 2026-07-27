"""Automatic backup scheduler regression tests."""

import asyncio
import errno
import gzip
import json
from datetime import UTC, datetime

import pytest

from trip_tracker.config import Settings
from trip_tracker.services import backups


@pytest.mark.parametrize(
    ("filename", "expected_reason"),
    (
        ("mileage-logger-auto-backup-20260726-120000Z.json.gz", "scheduled"),
        ("mileage-logger-auto-backup-startup-20260726-120000Z.json.gz", "startup"),
        (
            "mileage-logger-auto-backup-emergency-20260726-120000Z.json.gz",
            "emergency",
        ),
        ("trip-tracker-auto-backup-20260726-120000Z.json.gz", "scheduled"),
    ),
)
def test_pre_1_5_and_current_automatic_backup_filenames_remain_available(
    filename: str,
    expected_reason: str,
) -> None:
    """Renaming the app must not hide retained automatic backup files."""

    parsed = backups._parse_automatic_backup_filename(filename)

    assert parsed == (datetime(2026, 7, 26, 12, tzinfo=UTC), expected_reason)


def test_pre_1_5_full_backup_format_remains_restore_compatible() -> None:
    """The renamed app accepts the trusted legacy format marker during validation."""

    content = gzip.compress(
        json.dumps(
            {
                "format": "mileage_logger.full_backup",
                "version": backups.BACKUP_VERSION,
            }
        ).encode("utf-8")
    )

    payload = backups._load_backup_payload(content)

    assert payload["format"] == "mileage_logger.full_backup"


def test_automatic_backup_scheduler_retries_stale_storage_until_success(
    monkeypatch,
    tmp_path,
) -> None:
    """A stale shared mount retries quickly, then resumes the normal interval."""

    settings = Settings(
        automatic_backup_dir=str(tmp_path / "backups"),
        automatic_backup_retry_seconds=60,
    )
    attempted_reasons: list[str] = []
    sleep_delays: list[float] = []

    monkeypatch.setattr(backups.database, "database_is_reachable", lambda: True)

    def fake_backup_once(_settings: Settings, *, reason: str):
        attempted_reasons.append(reason)
        if len(attempted_reasons) == 1:
            raise OSError(errno.ESTALE, "Stale file handle")
        return object()

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(backups, "run_automatic_backup_once", fake_backup_once)
    monkeypatch.setattr(backups.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backups.automatic_backup_scheduler(settings))

    assert attempted_reasons == ["startup", "startup"]
    assert sleep_delays == [60, backups.AUTOMATIC_BACKUP_INTERVAL_SECONDS]
