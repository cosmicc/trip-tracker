"""Monthly reimbursement workflow and shared protection for submitted report data."""

import hashlib
import json
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy import and_, event, func, inspect, or_, select, text
from sqlalchemy.orm import Session, joinedload

from trip_tracker.config import get_settings
from trip_tracker.models import (
    DeletedTrip,
    MonthlyGasPrice,
    MonthlyReportExpense,
    MonthlyReportStatus,
    Site,
    Trip,
)
from trip_tracker.services.timezone import local_today


def lock_report_writes(db: Session) -> None:
    """Serialize report submissions and data writes across PostgreSQL app processes.

    The transaction-scoped advisory lock is released automatically on commit/rollback.
    SQLite test databases serialize their writes themselves.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(1530001)"))


def report_status(db: Session, year: int, month: int) -> MonthlyReportStatus | None:
    """Read fresh submission state, including changes from other app processes."""
    return db.scalar(
        select(MonthlyReportStatus)
        .where(
            MonthlyReportStatus.year == year,
            MonthlyReportStatus.month == month,
        )
        .execution_options(populate_existing=True)
    )


def month_is_submitted(db: Session, year: int, month: int) -> bool:
    """Return whether report data for this month is locked."""
    status = report_status(db, year, month)
    return status is not None and status.submitted_at is not None


def require_month_editable(db: Session, year: int, month: int) -> None:
    """Reject changes to submitted months, including indirectly changed odometers."""
    lock_report_writes(db)
    if month_is_submitted(db, year, month):
        raise HTTPException(
            409,
            f"{year}-{month:02d} is submitted. Mark it unsubmitted before editing report data.",
        )


def report_fingerprint(db: Session, year: int, month: int) -> str:
    """Hash the saved report inputs so edits require a fresh PDF download."""
    settings = get_settings()
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    data = [str(settings.vehicle_mpg), settings.report_display_name, settings.gas_price_state]
    for model, criteria in (
        (Trip, (Trip.trip_date >= start, Trip.trip_date < end)),
        (
            MonthlyReportExpense,
            (MonthlyReportExpense.year == year, MonthlyReportExpense.month == month),
        ),
        (
            MonthlyGasPrice,
            (
                MonthlyGasPrice.year == year,
                MonthlyGasPrice.month == month,
                MonthlyGasPrice.state == settings.gas_price_state.upper(),
            ),
        ),
    ):
        rows = db.execute(select(model.__table__).where(*criteria).order_by(model.id)).mappings()
        data.append(
            [
                {
                    key: str(value)
                    for key, value in row.items()
                    if key not in {"created_at", "updated_at"}
                }
                for row in rows
            ]
        )
    # Legacy rows may display a waypoint's current name instead of a stored trip name.
    trips = db.scalars(
        select(Trip)
        .options(
            joinedload(Trip.origin_site),
            joinedload(Trip.destination_site),
        )
        .where(Trip.trip_date >= start, Trip.trip_date < end)
        .order_by(Trip.id)
    )
    data.append(
        [(trip.id, trip.origin_display_name, trip.destination_display_name) for trip in trips]
    )
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def record_report_download(db: Session, year: int, month: int) -> None:
    """Record a successfully generated PDF response, never browser filesystem access."""
    lock_report_writes(db)
    status = report_status(db, year, month)
    if status is None:
        status = MonthlyReportStatus(year=year, month=month)
        db.add(status)
    status.downloaded_at = datetime.now(UTC)
    status.download_fingerprint = report_fingerprint(db, year, month)
    db.commit()


def set_report_submitted(db: Session, year: int, month: int, submitted: bool) -> None:
    """Submit a completed downloaded report, or unlock it and require a fresh PDF."""
    date(year, month, 1)  # Reject invalid dates before accessing persistent state.
    lock_report_writes(db)
    status = report_status(db, year, month)
    if submitted:
        if date(year, month, 1) >= local_today().replace(day=1):
            raise HTTPException(409, "The report month must finish before it can be submitted.")
        if (
            status is None
            or status.downloaded_at is None
            or status.download_fingerprint != report_fingerprint(db, year, month)
        ):
            raise HTTPException(
                409, "Download a fresh PDF for this month before marking it submitted."
            )
        if status.submitted_at is None:
            status.submitted_at = datetime.now(UTC)
    elif status is not None:
        status.submitted_at = None
        status.downloaded_at = None
        status.download_fingerprint = None
    db.commit()


@event.listens_for(Session, "before_flush")
def protect_submitted_report_rows(db: Session, _context: object, _instances: object) -> None:
    """Enforce report locks for every ORM writer, including maintenance and API paths."""
    months: set[tuple[int, int]] = set()
    for row in db.new.union(db.dirty).union(db.deleted):
        if isinstance(row, Site) and row.id is not None:
            if row not in db.deleted and not inspect(row).attrs.name.history.has_changes():
                continue
            affected = db.scalars(
                select(Trip).where(
                    or_(
                        and_(
                            Trip.origin_site_id == row.id,
                            or_(Trip.origin_name.is_(None), func.trim(Trip.origin_name) == ""),
                        ),
                        and_(
                            Trip.destination_site_id == row.id,
                            or_(
                                Trip.destination_name.is_(None),
                                func.trim(Trip.destination_name) == "",
                            ),
                        ),
                    )
                )
            )
            months.update((trip.trip_date.year, trip.trip_date.month) for trip in affected)
            continue
        if not isinstance(row, (Trip, DeletedTrip, MonthlyReportExpense, MonthlyGasPrice)):
            continue
        if row in db.dirty and not db.is_modified(row, include_collections=False):
            continue
        if isinstance(row, (Trip, DeletedTrip)):
            dates = [row.trip_date, *inspect(row).attrs.trip_date.history.deleted]
            months.update((day.year, day.month) for day in dates if day is not None)
        else:
            years = {row.year, *inspect(row).attrs.year.history.deleted}
            month_numbers = {row.month, *inspect(row).attrs.month.history.deleted}
            months.update((year, month) for year in years for month in month_numbers)
    for year, month in sorted(months):
        require_month_editable(db, year, month)
