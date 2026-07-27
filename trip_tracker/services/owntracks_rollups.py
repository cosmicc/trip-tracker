"""Persistent monthly OwnTracks-derived summaries."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trip_tracker.models import OwnTracksLocation, OwnTracksMonthlySummary, Site
from trip_tracker.services.mileage import owntracks_segment_miles, site_indexes
from trip_tracker.services.timezone import datetime_to_local_date, local_day_bounds

DISTANCE_PRECISION = Decimal("0.1")


def month_date_bounds(year: int, month: int) -> tuple[date, date]:
    """Return inclusive and exclusive local dates for a month."""

    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start_date, end_date


def month_datetime_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Return UTC datetime bounds for a complete app-local month."""

    start_date, end_date = month_date_bounds(year, month)
    start_dt, _ = local_day_bounds(start_date)
    end_dt, _ = local_day_bounds(end_date)
    return start_dt, end_dt


def _quantize_distance(value: Decimal) -> Decimal:
    """Round OwnTracks monthly totals to the displayed one-decimal precision."""

    return Decimal(value).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _owntracks_location_before(db: Session, before_dt: datetime) -> OwnTracksLocation | None:
    """Return the latest OwnTracks row before a UTC boundary."""

    return db.scalar(
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at < before_dt)
        .order_by(OwnTracksLocation.captured_at.desc(), OwnTracksLocation.id.desc())
        .limit(1)
    )


def _owntracks_locations_in_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return OwnTracks rows inside a UTC range in chronological order."""

    return list(
        db.scalars(
            select(OwnTracksLocation)
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
            .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
        )
    )


def owntracks_path_locations_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return path rows used to count segments ending inside the UTC range."""

    previous_location = _owntracks_location_before(db, start_dt)
    locations = _owntracks_locations_in_range(db, start_dt, end_dt)
    if previous_location is None:
        return locations
    return [previous_location, *locations]


def owntracks_total_miles_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> Decimal:
    """Calculate total driven distance directly from retained OwnTracks coordinate points."""

    path_locations = owntracks_path_locations_for_datetime_range(db, start_dt, end_dt)
    if len(path_locations) < 2:
        return Decimal("0.0")

    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    total_miles = Decimal("0.0")
    previous_location = path_locations[0]
    for location in path_locations[1:]:
        total_miles += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location

    return _quantize_distance(total_miles)


def owntracks_event_count_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> int:
    """Count retained OwnTracks rows captured inside a half-open UTC datetime range."""

    return int(
        db.scalar(
            select(func.count(OwnTracksLocation.id))
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
        )
        or 0
    )


def _raw_owntracks_event_count_for_month(db: Session, year: int, month: int) -> int:
    """Count retained OwnTracks rows for a month without consulting stored summaries."""

    start_dt, end_dt = month_datetime_bounds(year, month)
    return int(
        db.scalar(
            select(func.count(OwnTracksLocation.id))
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
        )
        or 0
    )


def _raw_owntracks_total_miles_for_month(db: Session, year: int, month: int) -> Decimal:
    """Calculate retained raw OwnTracks distance for one local month."""

    start_dt, end_dt = month_datetime_bounds(year, month)
    return owntracks_total_miles_for_datetime_range(db, start_dt, end_dt)


def _owntracks_summary_for_month(
    db: Session,
    *,
    year: int,
    month: int,
) -> OwnTracksMonthlySummary | None:
    """Load the persisted summary for one local month, if it exists."""

    return db.scalar(
        select(OwnTracksMonthlySummary)
        .where(OwnTracksMonthlySummary.year == year)
        .where(OwnTracksMonthlySummary.month == month)
        .limit(1)
    )


def refresh_owntracks_monthly_summary(
    db: Session,
    *,
    year: int,
    month: int,
) -> OwnTracksMonthlySummary:
    """Persist the highest observed OwnTracks-derived totals for one local month."""

    raw_total_miles = _raw_owntracks_total_miles_for_month(db, year, month)
    raw_event_count = _raw_owntracks_event_count_for_month(db, year, month)
    summary = _owntracks_summary_for_month(db, year=year, month=month)
    if summary is None:
        summary = OwnTracksMonthlySummary(
            year=year,
            month=month,
            total_miles=raw_total_miles,
            event_count=raw_event_count,
            source="owntracks_retention_rollup",
        )
        db.add(summary)
        return summary

    summary.total_miles = max(Decimal(summary.total_miles), raw_total_miles).quantize(
        DISTANCE_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    summary.event_count = max(int(summary.event_count), raw_event_count)
    summary.source = "owntracks_retention_rollup"
    return summary


def refresh_owntracks_monthly_summaries_before_purge(
    db: Session,
    *,
    checkpoint_location_id: int,
    cutoff_dt: datetime,
) -> int:
    """Refresh summaries for months with raw OwnTracks rows about to be purged."""

    purge_candidate_times = db.scalars(
        select(OwnTracksLocation.captured_at)
        .where(OwnTracksLocation.id <= checkpoint_location_id)
        .where(OwnTracksLocation.captured_at < cutoff_dt)
    )
    touched_months = set()
    for captured_at in purge_candidate_times:
        local_date = datetime_to_local_date(captured_at)
        touched_months.add((local_date.year, local_date.month))
    for year, month in sorted(touched_months):
        refresh_owntracks_monthly_summary(db, year=year, month=month)
    return len(touched_months)


def owntracks_monthly_total_miles(db: Session, *, year: int, month: int) -> Decimal:
    """Return stable OwnTracks-derived monthly distance using raw rows or stored rollup."""

    raw_total = _raw_owntracks_total_miles_for_month(db, year, month)
    summary = _owntracks_summary_for_month(db, year=year, month=month)
    if summary is None:
        return raw_total
    return max(Decimal(summary.total_miles), raw_total).quantize(
        DISTANCE_PRECISION,
        rounding=ROUND_HALF_UP,
    )


def owntracks_monthly_event_count(db: Session, *, year: int, month: int) -> int:
    """Return stable OwnTracks event count using raw rows or stored rollup."""

    raw_count = _raw_owntracks_event_count_for_month(db, year, month)
    summary = _owntracks_summary_for_month(db, year=year, month=month)
    if summary is None:
        return raw_count
    return max(int(summary.event_count), raw_count)
