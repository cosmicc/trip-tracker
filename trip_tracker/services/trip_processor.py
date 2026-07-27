import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from threading import Event, Lock, Thread

from sqlalchemy import select
from sqlalchemy.orm import Session

from trip_tracker.config import get_settings
from trip_tracker.database import SessionLocal, database_is_reachable
from trip_tracker.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    OwnTracksLocation,
    Site,
    TripProcessingCheckpoint,
)
from trip_tracker.services.mileage import (
    ODOMETER_SOURCE_OWNTRACKS_ROLLING,
    backfill_missing_trip_odometers,
    generate_trips,
    owntracks_segment_miles,
    resequence_all_trip_odometers_to_manual_reading,
    site_indexes,
)
from trip_tracker.services.retention import RetentionResult, purge_processed_owntracks_locations
from trip_tracker.services.timezone import datetime_to_local_date, datetime_to_utc

logger = logging.getLogger(__name__)
trip_logger = logging.getLogger("trip_tracker.trip_calculation")
_PROCESSING_LOCK = Lock()
ODOMETER_PRECISION = Decimal("0.1")


@dataclass(frozen=True)
class TripProcessingResult:
    generated: int
    retention: RetentionResult
    processed_dates: tuple[date, ...]
    processed_location_count: int = 0
    checkpoint_location_id: int | None = None
    repaired_trip_count: int = 0

    @property
    def monthly_reset(self) -> RetentionResult:
        return self.retention


@dataclass(frozen=True)
class OdometerAdvance:
    """Distance and timestamp used to update the rolling OwnTracks odometer estimate."""

    miles: Decimal
    recorded_at: datetime | None


def _ensure_checkpoint_table(db: Session) -> None:
    TripProcessingCheckpoint.__table__.create(bind=db.get_bind(), checkfirst=True)


def _get_or_create_checkpoint(db: Session) -> TripProcessingCheckpoint:
    _ensure_checkpoint_table(db)
    checkpoint = db.scalar(
        select(TripProcessingCheckpoint).where(
            TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT
        )
    )
    if checkpoint is not None:
        return checkpoint

    checkpoint = TripProcessingCheckpoint(name=AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
    db.add(checkpoint)
    db.flush()
    return checkpoint


def _new_locations_after_checkpoint(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
) -> list[OwnTracksLocation]:
    stmt = select(OwnTracksLocation).order_by(OwnTracksLocation.id.asc())
    if checkpoint.last_owntracks_location_id is not None:
        stmt = stmt.where(OwnTracksLocation.id > checkpoint.last_owntracks_location_id)
    return list(db.scalars(stmt))


def _quantize_odometer(value: Decimal) -> Decimal:
    """Round odometer values to the precision used by the rolling odometer checkpoint."""

    return Decimal(str(value)).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)


def update_odometer_anchor_from_manual_reading(
    db: Session,
    odometer_miles: Decimal,
    *,
    recorded_at: datetime,
    align_trip_odometers: bool = False,
) -> TripProcessingCheckpoint:
    """Update the rolling checkpoint from an explicit manual odometer reading.

    When the caller confirms the vehicle is inside Home, all trip display odometers are aligned
    to the reading in the same transaction. No trip-derived value is written to the checkpoint.
    """

    adjusted_trip_count = 0
    if align_trip_odometers:
        adjusted_trip_count = resequence_all_trip_odometers_to_manual_reading(
            db,
            odometer_miles,
        )
    checkpoint = _get_or_create_checkpoint(db)
    normalized_odometer = _quantize_odometer(odometer_miles)
    checkpoint.odometer_anchor_miles = normalized_odometer
    checkpoint.odometer_anchor_recorded_at = datetime_to_utc(recorded_at)
    db.commit()
    trip_logger.info(
        "Updated odometer anchor from manual reading miles=%s recorded_at=%s adjusted_trips=%s",
        normalized_odometer,
        datetime_to_utc(recorded_at).isoformat(),
        adjusted_trip_count,
    )
    return checkpoint


def _ensure_initial_odometer_anchor(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
    *,
    current_dt: datetime,
    new_locations: list[OwnTracksLocation],
) -> None:
    """Create the first rolling odometer anchor from stored data or a zero-mile baseline."""

    if checkpoint.odometer_anchor_miles is not None:
        return

    earliest_new_location = min(new_locations, key=_location_sort_key) if new_locations else None
    checkpoint.odometer_anchor_miles = Decimal("0.0")
    checkpoint.odometer_anchor_recorded_at = (
        datetime_to_utc(earliest_new_location.captured_at) - timedelta(microseconds=1)
        if earliest_new_location is not None
        else current_dt
    )
    trip_logger.info(
        "Saved initial OwnTracks odometer anchor miles=%s recorded_at=%s",
        checkpoint.odometer_anchor_miles,
        checkpoint.odometer_anchor_recorded_at.isoformat(),
    )


def _location_sort_key(location: OwnTracksLocation) -> tuple[datetime, int]:
    """Return a stable chronological key based on OwnTracks event time, not receive time."""

    return datetime_to_utc(location.captured_at), location.id or 0


def _owntracks_odometer_advance(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
    new_locations: list[OwnTracksLocation],
) -> OdometerAdvance:
    """Calculate uncounted OwnTracks path distance after the current odometer anchor."""

    if not new_locations:
        return OdometerAdvance(miles=Decimal("0.0"), recorded_at=None)

    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    new_location_ids = {location.id for location in new_locations}
    path_locations = list(new_locations)
    anchor_recorded_at = (
        datetime_to_utc(checkpoint.odometer_anchor_recorded_at)
        if checkpoint.odometer_anchor_recorded_at is not None
        else None
    )

    if checkpoint.last_owntracks_location_id is not None:
        previous_checkpoint_location = db.get(
            OwnTracksLocation,
            checkpoint.last_owntracks_location_id,
        )
        if previous_checkpoint_location is not None:
            path_locations.append(previous_checkpoint_location)

    total_miles = Decimal("0.0")
    current_odometer = Decimal(checkpoint.odometer_anchor_miles or Decimal("0.0")).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    latest_counted_at: datetime | None = None
    previous_location: OwnTracksLocation | None = None

    for location in sorted(path_locations, key=_location_sort_key):
        location_time = datetime_to_utc(location.captured_at)
        location_is_new = location.id in new_location_ids
        location_is_after_anchor = anchor_recorded_at is None or location_time > anchor_recorded_at

        if location_is_new and location_is_after_anchor:
            segment_miles = Decimal("0.0")
            latest_counted_at = (
                location_time
                if latest_counted_at is None
                else max(latest_counted_at, location_time)
            )
            if previous_location is not None:
                segment_miles = owntracks_segment_miles(
                    previous_location,
                    location,
                    sites,
                    sites_by_name,
                    sites_by_region_id,
                )
                total_miles += segment_miles
            current_odometer = _quantize_odometer(current_odometer + segment_miles)
            location.odometer_miles = current_odometer
            location.odometer_source = ODOMETER_SOURCE_OWNTRACKS_ROLLING

        previous_location = location

    return OdometerAdvance(
        miles=total_miles.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP),
        recorded_at=latest_counted_at,
    )


def _advance_odometer_anchor_from_owntracks(
    db: Session,
    checkpoint: TripProcessingCheckpoint,
    new_locations: list[OwnTracksLocation],
) -> None:
    """Advance the rolling odometer with OwnTracks distance that does not become a trip."""

    if checkpoint.odometer_anchor_miles is None:
        trip_logger.debug("OwnTracks odometer advance skipped because no odometer anchor exists")
        return

    odometer_advance = _owntracks_odometer_advance(db, checkpoint, new_locations)
    if odometer_advance.recorded_at is None:
        return

    checkpoint.odometer_anchor_miles = _quantize_odometer(
        checkpoint.odometer_anchor_miles + odometer_advance.miles
    )
    checkpoint.odometer_anchor_recorded_at = odometer_advance.recorded_at
    trip_logger.info(
        "Advanced odometer anchor from OwnTracks miles_added=%s odometer=%s recorded_at=%s",
        odometer_advance.miles,
        checkpoint.odometer_anchor_miles,
        odometer_advance.recorded_at.isoformat(),
    )


def _generate_for_date(
    db: Session,
    day: date,
    processed_dates: list[date],
    *,
    as_of: datetime,
) -> int:
    if day in processed_dates:
        return 0
    processed_dates.append(day)
    return len(generate_trips(db, day, day, as_of=as_of))


def _dates_touched_by_new_locations(locations: list[OwnTracksLocation]) -> set[date]:
    touched_dates: set[date] = set()
    for location in locations:
        location_date = datetime_to_local_date(location.captured_at)
        touched_dates.add(location_date)
        touched_dates.add(location_date - timedelta(days=1))
    return touched_dates


def run_automatic_trip_processing(
    db: Session,
    *,
    touched_date: date | None = None,
    now: datetime | None = None,
    finalize_completed_days: bool = True,
) -> TripProcessingResult:
    current_dt = now or datetime.now(UTC)
    today = datetime_to_local_date(current_dt)
    generated = 0
    retention = RetentionResult(location_points=0, trips=0, gas_snapshots=0)
    processed_dates: list[date] = []
    processed_location_count = 0
    checkpoint_location_id: int | None = None
    repaired_trip_count = 0

    with _PROCESSING_LOCK:
        trip_logger.info(
            "automatic trip processing started touched_date=%s now=%s finalize_completed_days=%s",
            touched_date.isoformat() if touched_date is not None else "",
            current_dt.isoformat(),
            finalize_completed_days,
        )
        checkpoint = _get_or_create_checkpoint(db)
        checkpoint_location_id = checkpoint.last_owntracks_location_id

        new_locations = _new_locations_after_checkpoint(db, checkpoint)
        _ensure_initial_odometer_anchor(
            db,
            checkpoint,
            current_dt=current_dt,
            new_locations=new_locations,
        )
        _advance_odometer_anchor_from_owntracks(db, checkpoint, new_locations)
        if new_locations:
            checkpoint.last_owntracks_location_id = max(location.id for location in new_locations)
            checkpoint_location_id = checkpoint.last_owntracks_location_id
            processed_location_count = len(new_locations)

        dates_to_process = _dates_touched_by_new_locations(new_locations)
        dates_to_process.add(today)
        dates_to_process.add(today - timedelta(days=1))
        if touched_date is not None:
            dates_to_process.add(touched_date)
            dates_to_process.add(touched_date - timedelta(days=1))

        if not finalize_completed_days:
            dates_to_process = {day for day in dates_to_process if day <= today}

        for day in sorted(dates_to_process):
            generated += _generate_for_date(db, day, processed_dates, as_of=current_dt)

        repaired_trip_count = backfill_missing_trip_odometers(db)
        if repaired_trip_count:
            db.commit()

        if new_locations:
            db.commit()

        retention = purge_processed_owntracks_locations(
            db,
            checkpoint_location_id=checkpoint.last_owntracks_location_id,
            now=current_dt,
        )

    result = TripProcessingResult(
        generated=generated,
        retention=retention,
        processed_dates=tuple(processed_dates),
        processed_location_count=processed_location_count,
        checkpoint_location_id=checkpoint_location_id,
        repaired_trip_count=repaired_trip_count,
    )
    trip_logger.info(
        "automatic trip processing complete generated=%s purged_location_points=%s "
        "purged_trips=%s purged_gas_snapshots=%s processed_locations=%s checkpoint_location_id=%s "
        "repaired_trip_odometers=%s dates=%s",
        result.generated,
        result.retention.location_points,
        result.retention.trips,
        result.retention.gas_snapshots,
        result.processed_location_count,
        result.checkpoint_location_id or "",
        result.repaired_trip_count,
        ",".join(day.isoformat() for day in result.processed_dates),
    )
    return result


class AutomaticTripProcessor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.automatic_trip_processing_enabled:
            logger.info("Automatic trip processing is disabled")
            return
        if self._thread is not None:
            logger.debug("Automatic trip processor already running")
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="automatic-trip-processor", daemon=True)
        self._thread.start()
        logger.info(
            "Automatic trip processor started interval_seconds=%s",
            self.settings.automatic_trip_processing_interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            logger.info("Automatic trip processor stopped")

    def _run(self) -> None:
        self._process_once()
        while not self._stop.wait(self.settings.automatic_trip_processing_interval_seconds):
            self._process_once()

    def _process_once(self) -> None:
        if not database_is_reachable():
            logger.info("Automatic trip processing paused because database is unavailable")
            return
        with SessionLocal() as db:
            try:
                result = run_automatic_trip_processing(db)
            except Exception:
                logger.exception("Automatic trip processing failed")
                return

        if result.generated or result.retention.total or result.repaired_trip_count:
            logger.info(
                "Automatic trip processing complete generated=%s "
                "purged_location_points=%s purged_trips=%s purged_gas_snapshots=%s "
                "repaired_trip_odometers=%s dates=%s",
                result.generated,
                result.retention.location_points,
                result.retention.trips,
                result.retention.gas_snapshots,
                result.repaired_trip_count,
                ",".join(day.isoformat() for day in result.processed_dates),
            )
