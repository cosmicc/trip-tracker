import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from trip_tracker.config import get_settings
from trip_tracker.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    DeletedTrip,
    OwnTracksLocation,
    Site,
    Trip,
    TripProcessingCheckpoint,
    normalize_location_name,
)
from trip_tracker.services.timezone import (
    datetime_to_local_date,
    datetime_to_utc,
    local_day_bounds,
    local_now,
)

METERS_PER_MILE = Decimal("1609.344")
EARTH_RADIUS_M = Decimal("6371008.8")
DISTANCE_PRECISION = Decimal("0.1")
ODOMETER_PRECISION = Decimal("0.1")
AUTO_TRIP_SOURCE = "auto"
MANUAL_TRIP_SOURCE = "manual"
MILEAGE_SOURCE_OWNTRACKS_PATH = "owntracks_path"
MILEAGE_SOURCE_ESTIMATED_ODOMETER = "estimated_odometer"
MILEAGE_SOURCE_WAYPOINT_DISTANCE = "waypoint_distance"
MILEAGE_SOURCE_MANUAL = "manual"
ODOMETER_SOURCE_MANUAL = "manual"
ODOMETER_SOURCE_ESTIMATED = "estimated"
ODOMETER_SOURCE_PREVIOUS_TRIP = "previous_trip"
ODOMETER_SOURCE_OWNTRACKS_ROLLING = "owntracks_rolling"
HOME_WAYPOINT_NAME = "Home"
SAME_WAYPOINT_MINIMUM_TRIP_MILES = Decimal("1.0")
EMERGENCY_MAX_PRESERVED_GAP_MILES = Decimal("200.0")
TRIP_END_CROSS_DAY_LOOKAHEAD = timedelta(days=1)
WAYPOINT_DEPARTURE_CONFIRMATION_DISTANCE_M = Decimal("500")
WAYPOINT_TRIP_NOTE = "Auto-generated from OwnTracks waypoint transitions."
MISSING_LEAVE_NOTE = "Missing leave event inferred from previous waypoint."
MANUAL_TRIP_NOTE = "Manually added from Trips page."
USER_EDITED_TRIP_NOTE = "Edited from Work Trips page."
trip_logger = logging.getLogger("trip_tracker.trip_calculation")
TripGenerationKey = tuple[int, int, datetime, datetime]
TripRecordedValuesKey = tuple[date, int, int, Decimal, Decimal, Decimal]


@dataclass(frozen=True)
class WaypointTransition:
    event: str
    site: Site
    location: OwnTracksLocation


@dataclass(frozen=True)
class MileageCalculation:
    miles: Decimal
    mileage_source: str
    start_odometer_miles: Decimal | None = None
    end_odometer_miles: Decimal | None = None
    start_odometer_source: str | None = None
    end_odometer_source: str | None = None


@dataclass(frozen=True)
class OdometerReading:
    miles: Decimal
    source: str


@dataclass(frozen=True)
class TimedOdometerReading(OdometerReading):
    recorded_at: datetime
    order_id: int


class TripOdometerAdjustmentError(ValueError):
    """Raised when trip odometers cannot be aligned to a manual reading safely."""


@dataclass(frozen=True)
class EmergencyOdometerRebuildResult:
    """Summary of one best-effort emergency trip odometer reconstruction."""

    adjusted_trip_count: int
    preserved_gap_count: int
    reconstructed_gap_count: int
    discarded_gap_count: int


def haversine_miles(
    lat1: Decimal | float,
    lon1: Decimal | float,
    lat2: Decimal | float,
    lon2: Decimal | float,
) -> Decimal:
    meters = haversine_meters(lat1, lon1, lat2, lon2)
    miles = meters / METERS_PER_MILE
    return miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def haversine_meters(
    lat1: Decimal | float,
    lon1: Decimal | float,
    lat2: Decimal | float,
    lon2: Decimal | float,
) -> Decimal:
    """Calculate unrounded distance between two GPS points in meters."""

    lat1_f = radians(float(lat1))
    lat2_f = radians(float(lat2))
    dlat = lat2_f - lat1_f
    dlon = radians(float(lon2) - float(lon1))
    a = sin(dlat / 2) ** 2 + cos(lat1_f) * cos(lat2_f) * sin(dlon / 2) ** 2
    meters = float(EARTH_RADIUS_M) * 2 * asin(sqrt(a))
    return Decimal(str(meters))


def distance_meters(site: Site, location: OwnTracksLocation) -> Decimal:
    return haversine_meters(
        site.latitude,
        site.longitude,
        location.latitude,
        location.longitude,
    )


def nearest_site(location: OwnTracksLocation, sites: list[Site]) -> Site | None:
    matches = [
        (distance_meters(site, location), site)
        for site in sites
        if distance_meters(site, location) <= Decimal(site.radius_m)
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def _date_range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt, _ = local_day_bounds(start_date)
    _, end_dt = local_day_bounds(end_date)
    return start_dt, end_dt


def date_bounds(day: date) -> tuple[datetime, datetime]:
    return local_day_bounds(day)


def _locations_for_range(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    end_padding: timedelta | None = None,
) -> list[OwnTracksLocation]:
    start_dt, end_dt = _date_range_bounds(start_date, end_date)
    if end_padding is not None:
        end_dt += end_padding
    stmt = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at >= start_dt)
        .where(OwnTracksLocation.captured_at < end_dt)
        .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
    )
    return list(db.scalars(stmt))


def _transition_event(location: OwnTracksLocation) -> str | None:
    payload = location.raw_payload or {}
    if payload.get("_type") != "transition":
        return None

    event = str(payload.get("event") or "").strip().casefold()
    if event in {"enter", "arrive", "arrival"}:
        return "enter"
    if event in {"leave", "exit", "departure"}:
        return "leave"
    return None


def _region_id(location: OwnTracksLocation) -> str | None:
    region_id = (location.raw_payload or {}).get("rid")
    if region_id is None:
        return None
    return str(region_id).strip() or None


def _region_names(location: OwnTracksLocation) -> list[str]:
    payload = location.raw_payload or {}
    names: list[str] = []
    description = payload.get("desc")
    if description:
        names.append(str(description))
    regions = payload.get("inregions")
    if isinstance(regions, list):
        names.extend(str(region) for region in regions if str(region).strip())
    return [name.strip() for name in names if name.strip()]


def _location_sort_key(location: OwnTracksLocation) -> tuple[datetime, int]:
    """Return a stable chronological key based on OwnTracks event time."""

    return datetime_to_utc(location.captured_at), location.id or 0


def site_indexes(sites: list[Site]) -> tuple[dict[str, Site], dict[str, Site]]:
    """Build saved-waypoint lookup maps for OwnTracks region and name matching."""

    sites_by_name = {site.name.casefold(): site for site in sites}
    sites_by_region_id = {
        site.owntracks_region_id: site
        for site in sites
        if site.owntracks_region_id is not None
    }
    return sites_by_name, sites_by_region_id


def site_for_location(
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Site | None:
    region_id = _region_id(location)
    if region_id is not None and region_id in sites_by_region_id:
        return sites_by_region_id[region_id]

    for region_name in _region_names(location):
        site = sites_by_name.get(region_name.casefold())
        if site is not None:
            return site
    return nearest_site(location, sites)


def same_saved_waypoint(
    first_location: OwnTracksLocation,
    second_location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> bool:
    """Return true when two OwnTracks rows are inside the same saved waypoint."""

    first_site = site_for_location(first_location, sites, sites_by_name, sites_by_region_id)
    second_site = site_for_location(second_location, sites, sites_by_name, sites_by_region_id)
    return first_site is not None and second_site is not None and first_site.id == second_site.id


def owntracks_segment_miles(
    first_location: OwnTracksLocation,
    second_location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal:
    """Return one OwnTracks movement segment while ignoring same-waypoint GPS drift."""

    if same_saved_waypoint(
        first_location,
        second_location,
        sites,
        sites_by_name,
        sites_by_region_id,
    ):
        return Decimal("0.0")
    return haversine_miles(
        first_location.latitude,
        first_location.longitude,
        second_location.latitude,
        second_location.longitude,
    )


def _coordinates_inside_site(site: Site, location: OwnTracksLocation) -> bool:
    """Return true when stored coordinates are physically inside the saved waypoint radius."""

    return distance_meters(site, location) <= Decimal(site.radius_m)


def _coordinates_clearly_away_from_site(site: Site, location: OwnTracksLocation) -> bool:
    """Return true when coordinates are far enough from a waypoint to disprove dwell."""

    confirmation_distance = max(
        Decimal(site.radius_m),
        WAYPOINT_DEPARTURE_CONFIRMATION_DISTANCE_M,
    )
    return distance_meters(site, location) >= confirmation_distance


def _payload_identifies_site(location: OwnTracksLocation, site: Site) -> bool:
    """Return true when OwnTracks explicitly labels a row as belonging to a saved site."""

    region_id = _region_id(location)
    if (
        region_id is not None
        and site.owntracks_region_id is not None
        and region_id == site.owntracks_region_id
    ):
        return True

    site_name = site.name.casefold()
    return any(region_name.casefold() == site_name for region_name in _region_names(location))


def _enter_transition_confirmed(
    enter_location: OwnTracksLocation,
    site: Site,
    locations: list[OwnTracksLocation],
    enter_index: int,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
    *,
    as_of: datetime | None,
) -> bool:
    """Confirm an enter event when OwnTracks rows prove destination dwell."""

    dwell_time = timedelta(minutes=get_settings().owntracks_waypoint_dwell_minutes)
    enter_time = datetime_to_utc(enter_location.captured_at)
    dwell_deadline = enter_time + dwell_time
    arrival_coordinates_inside = _coordinates_inside_site(site, enter_location)

    if not arrival_coordinates_inside and not _payload_identifies_site(enter_location, site):
        trip_logger.debug(
            "waypoint enter rejected reason=enter_unmatched_outside_radius site=%s "
            "captured_at=%s distance_m=%s radius_m=%s",
            site.name,
            enter_location.captured_at.isoformat(),
            distance_meters(site, enter_location).quantize(Decimal("0.1")),
            site.radius_m,
        )
        return False

    if not arrival_coordinates_inside:
        trip_logger.debug(
            "waypoint enter needs state confirmation reason=enter_coordinates_outside_radius "
            "site=%s captured_at=%s distance_m=%s radius_m=%s",
            site.name,
            enter_location.captured_at.isoformat(),
            distance_meters(site, enter_location).quantize(Decimal("0.1")),
            site.radius_m,
        )

    for candidate_location in locations[enter_index + 1 :]:
        candidate_time = datetime_to_utc(candidate_location.captured_at)
        candidate_event = _transition_event(candidate_location)
        candidate_site = site_for_location(
            candidate_location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )

        if (
            candidate_event == "leave"
            and candidate_site is not None
            and candidate_site.id == site.id
        ):
            if candidate_time >= dwell_deadline:
                if not arrival_coordinates_inside:
                    trip_logger.info(
                        "waypoint enter confirmed reason=outside_enter_then_same_site_leave "
                        "site=%s captured_at=%s leave_at=%s dwell_deadline=%s",
                        site.name,
                        enter_location.captured_at.isoformat(),
                        candidate_location.captured_at.isoformat(),
                        dwell_deadline.isoformat(),
                    )
                return True
            return False

        if (
            candidate_event == "enter"
            and candidate_site is not None
            and candidate_site.id != site.id
        ):
            if candidate_time >= dwell_deadline and arrival_coordinates_inside:
                return True
            return False

        if candidate_time >= dwell_deadline and _coordinates_inside_site(site, candidate_location):
            return True

        if (
            candidate_time < dwell_deadline
            and candidate_event is None
            and _coordinates_clearly_away_from_site(site, candidate_location)
        ):
            trip_logger.debug(
                "waypoint enter rejected reason=left_before_dwell site=%s captured_at=%s "
                "candidate_at=%s distance_m=%s dwell_deadline=%s",
                site.name,
                enter_location.captured_at.isoformat(),
                candidate_location.captured_at.isoformat(),
                distance_meters(site, candidate_location).quantize(Decimal("0.1")),
                dwell_deadline.isoformat(),
            )
            return False

    if (
        as_of is not None
        and arrival_coordinates_inside
        and datetime_to_utc(as_of) >= dwell_deadline
    ):
        return True

    if as_of is not None and datetime_to_utc(as_of) >= dwell_deadline:
        trip_logger.debug(
            "waypoint enter rejected reason=outside_radius_without_confirming_state site=%s "
            "captured_at=%s dwell_deadline=%s as_of=%s",
            site.name,
            enter_location.captured_at.isoformat(),
            dwell_deadline.isoformat(),
            datetime_to_utc(as_of).isoformat(),
        )
    elif as_of is not None:
        trip_logger.debug(
            "waypoint enter pending reason=awaiting_coordinate_confirmation site=%s "
            "captured_at=%s dwell_deadline=%s as_of=%s",
            site.name,
            enter_location.captured_at.isoformat(),
            dwell_deadline.isoformat(),
            datetime_to_utc(as_of).isoformat(),
        )
    return False


def _waypoint_transitions(
    locations: list[OwnTracksLocation],
    sites: list[Site],
    *,
    as_of: datetime | None,
) -> list[WaypointTransition]:
    sites_by_name, sites_by_region_id = site_indexes(sites)
    transitions: list[WaypointTransition] = []
    seen: set[tuple[str, int, datetime]] = set()

    ordered_locations = sorted(locations, key=_location_sort_key)
    for location_index, location in enumerate(ordered_locations):
        event = _transition_event(location)
        if event is None:
            continue
        site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        if site is None:
            continue
        if event == "enter" and not _enter_transition_confirmed(
            location,
            site,
            ordered_locations,
            location_index,
            sites,
            sites_by_name,
            sites_by_region_id,
            as_of=as_of,
        ):
            trip_logger.debug(
                "waypoint enter skipped reason=dwell_not_confirmed site=%s captured_at=%s "
                "dwell_minutes=%s as_of=%s",
                site.name,
                location.captured_at.isoformat(),
                get_settings().owntracks_waypoint_dwell_minutes,
                datetime_to_utc(as_of).isoformat() if as_of is not None else "",
            )
            continue
        dedupe_key = (event, site.id, location.captured_at)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        transitions.append(WaypointTransition(event=event, site=site, location=location))

    return transitions


def _is_location_update(location: OwnTracksLocation) -> bool:
    payload = location.raw_payload or {}
    return payload.get("_type") == "location"


def _trip_path_locations(
    locations: list[OwnTracksLocation],
    *,
    started_at: datetime,
    ended_at: datetime,
) -> list[OwnTracksLocation]:
    start_dt = datetime_to_utc(started_at)
    end_dt = datetime_to_utc(ended_at)
    if end_dt < start_dt:
        return []
    return [
        location
        for location in locations
        if start_dt <= datetime_to_utc(location.captured_at) <= end_dt
    ]


def _path_distance_miles(
    path_locations: list[OwnTracksLocation],
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal | None:
    """Return trip path distance using the same movement rule as the rolling odometer."""

    has_location_update = any(
        _is_location_update(location) for location in path_locations
    )
    if len(path_locations) < 2 or not has_location_update:
        return None

    total = Decimal("0.0")
    previous_location = path_locations[0]
    for location in path_locations[1:]:
        total += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location
    return total.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _trip_path_miles(
    locations: list[OwnTracksLocation],
    *,
    started_at: datetime,
    ended_at: datetime,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Decimal | None:
    return _path_distance_miles(
        _trip_path_locations(locations, started_at=started_at, ended_at=ended_at),
        sites,
        sites_by_name,
        sites_by_region_id,
    )


def _home_site(sites: list[Site]) -> Site | None:
    for site in sites:
        if site.name == HOME_WAYPOINT_NAME:
            return site
    return None


def _trip_notes(*, inferred_leave: bool) -> str:
    if inferred_leave:
        return f"{WAYPOINT_TRIP_NOTE} {MISSING_LEAVE_NOTE}"
    return WAYPOINT_TRIP_NOTE


def _append_note(existing_notes: str | None, note: str) -> str:
    existing = (existing_notes or "").strip()
    if existing == note or existing.endswith(f" {note}"):
        return existing
    return f"{existing} {note}".strip() if existing else note


def _odometer_for_transition(
    _db: Session,
    transition: WaypointTransition,
) -> OdometerReading | None:
    """Return the rolling OwnTracks odometer stamped onto a transition row."""

    if transition.location.odometer_miles is None:
        return None
    return OdometerReading(
        miles=Decimal(transition.location.odometer_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        ),
        source=transition.location.odometer_source or ODOMETER_SOURCE_OWNTRACKS_ROLLING,
    )


def _is_home_to_home(origin: Site, destination: Site) -> bool:
    return origin.name == HOME_WAYPOINT_NAME and destination.name == HOME_WAYPOINT_NAME


def _trip_overlaps_window(trip: Trip, started_at: datetime, ended_at: datetime) -> bool:
    if started_at == ended_at:
        return trip.started_at <= started_at <= trip.ended_at
    return trip.started_at < ended_at and trip.ended_at > started_at


def _preserved_trips_for_range(db: Session, start_date: date, end_date: date) -> list[Trip]:
    return list(
        db.scalars(
            select(Trip)
            .where(
                or_(
                    Trip.source != AUTO_TRIP_SOURCE,
                    Trip.mileage_source == MILEAGE_SOURCE_MANUAL,
                )
            )
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date <= end_date)
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )


def _overlaps_preserved_trip(
    preserved_trips: list[Trip],
    *,
    trip_date: date,
    started_at: datetime,
    ended_at: datetime,
) -> bool:
    return any(
        trip.trip_date == trip_date and _trip_overlaps_window(trip, started_at, ended_at)
        for trip in preserved_trips
    )


def _odometer_reading_miles(reading: OdometerReading | None) -> Decimal | None:
    return reading.miles if reading is not None else None


def _odometer_reading_source(reading: OdometerReading | None) -> str | None:
    return reading.source if reading is not None else None


def _new_odometer_details_available(
    existing_trip: Trip,
    calculation: MileageCalculation,
) -> bool:
    """Return true when a recalculation adds or corrects stored odometer details."""

    existing_values = (
        existing_trip.start_odometer_miles,
        existing_trip.end_odometer_miles,
        existing_trip.start_odometer_source,
        existing_trip.end_odometer_source,
    )
    calculated_values = (
        calculation.start_odometer_miles,
        calculation.end_odometer_miles,
        calculation.start_odometer_source,
        calculation.end_odometer_source,
    )
    return existing_values != calculated_values and any(
        value is not None for value in calculated_values
    )


def _distance_estimate_miles(origin: Site, destination: Site) -> Decimal:
    return haversine_miles(
        origin.latitude,
        origin.longitude,
        destination.latitude,
        destination.longitude,
    )


def _estimated_odometer_calculation(
    distance_miles: Decimal,
    *,
    mileage_source: str,
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
    start_odometer_source: str | None,
    end_odometer_source: str | None,
    odometer_anchor_miles: Decimal | None,
    odometer_anchor_source: str | None,
) -> MileageCalculation | None:
    distance = distance_miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
    if start_odometer_miles is not None:
        estimated_end = (start_odometer_miles + distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=mileage_source,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=estimated_end,
            start_odometer_source=start_odometer_source or ODOMETER_SOURCE_PREVIOUS_TRIP,
            end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
        )

    if end_odometer_miles is not None:
        estimated_start = max(end_odometer_miles - distance, Decimal("0.0")).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        return MileageCalculation(
            miles=distance,
            mileage_source=mileage_source,
            start_odometer_miles=estimated_start,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=ODOMETER_SOURCE_ESTIMATED,
            end_odometer_source=end_odometer_source or ODOMETER_SOURCE_MANUAL,
        )

    if odometer_anchor_miles is None:
        return None

    estimated_start = odometer_anchor_miles.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
    estimated_end = (estimated_start + distance).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    return MileageCalculation(
        miles=distance,
        mileage_source=mileage_source,
        start_odometer_miles=estimated_start,
        end_odometer_miles=estimated_end,
        start_odometer_source=odometer_anchor_source or ODOMETER_SOURCE_PREVIOUS_TRIP,
        end_odometer_source=ODOMETER_SOURCE_ESTIMATED,
    )


def _mileage_calculation(
    origin: Site,
    destination: Site,
    *,
    path_miles: Decimal | None,
    start_odometer: OdometerReading | None,
    end_odometer: OdometerReading | None,
    odometer_anchor_miles: Decimal | None,
    odometer_anchor_source: str | None,
) -> MileageCalculation:
    start_odometer_miles = _odometer_reading_miles(start_odometer)
    end_odometer_miles = _odometer_reading_miles(end_odometer)
    start_odometer_source = _odometer_reading_source(start_odometer)
    end_odometer_source = _odometer_reading_source(end_odometer)

    if path_miles is not None:
        path_odometer = _estimated_odometer_calculation(
            path_miles,
            mileage_source=MILEAGE_SOURCE_OWNTRACKS_PATH,
            start_odometer_miles=start_odometer_miles,
            end_odometer_miles=end_odometer_miles,
            start_odometer_source=start_odometer_source,
            end_odometer_source=end_odometer_source,
            odometer_anchor_miles=odometer_anchor_miles,
            odometer_anchor_source=odometer_anchor_source,
        )
        if (
            path_odometer is not None
            and (
                start_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
                or end_odometer_source == ODOMETER_SOURCE_OWNTRACKS_ROLLING
            )
        ):
            path_odometer = MileageCalculation(
                miles=path_odometer.miles,
                mileage_source=path_odometer.mileage_source,
                start_odometer_miles=path_odometer.start_odometer_miles,
                end_odometer_miles=path_odometer.end_odometer_miles,
                start_odometer_source=(
                    path_odometer.start_odometer_source or ODOMETER_SOURCE_OWNTRACKS_ROLLING
                ),
                end_odometer_source=ODOMETER_SOURCE_OWNTRACKS_ROLLING,
            )
        return MileageCalculation(
            miles=path_miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP),
            mileage_source=MILEAGE_SOURCE_OWNTRACKS_PATH,
            start_odometer_miles=(
                path_odometer.start_odometer_miles
                if path_odometer is not None
                else start_odometer_miles
            ),
            end_odometer_miles=(
                path_odometer.end_odometer_miles
                if path_odometer is not None
                else end_odometer_miles
            ),
            start_odometer_source=(
                path_odometer.start_odometer_source
                if path_odometer is not None
                else start_odometer_source
            ),
            end_odometer_source=(
                path_odometer.end_odometer_source
                if path_odometer is not None
                else end_odometer_source
            ),
        )

    distance_miles = _distance_estimate_miles(origin, destination)
    estimated = _estimated_odometer_calculation(
        distance_miles,
        mileage_source=MILEAGE_SOURCE_WAYPOINT_DISTANCE,
        start_odometer_miles=start_odometer_miles,
        end_odometer_miles=end_odometer_miles,
        start_odometer_source=start_odometer_source,
        end_odometer_source=end_odometer_source,
        odometer_anchor_miles=odometer_anchor_miles,
        odometer_anchor_source=odometer_anchor_source,
    )
    if estimated is not None:
        return estimated

    return MileageCalculation(
        miles=distance_miles,
        mileage_source=MILEAGE_SOURCE_WAYPOINT_DISTANCE,
    )


def _site_latitude(site: Site) -> Decimal:
    return Decimal(site.latitude)


def _site_longitude(site: Site) -> Decimal:
    return Decimal(site.longitude)


def _is_same_waypoint_under_minimum_miles(origin: Site, destination: Site, miles: Decimal) -> bool:
    """Return true when a same-waypoint automatic trip is too short to be valid."""

    return origin.id == destination.id and miles < SAME_WAYPOINT_MINIMUM_TRIP_MILES


def _should_skip_for_minimum_miles(origin: Site, destination: Site, miles: Decimal) -> bool:
    if _is_same_waypoint_under_minimum_miles(origin, destination, miles):
        return True
    if origin.id == destination.id:
        return False
    return miles < get_settings().min_trip_miles


def _mileage_source_rank(value: str | None) -> int:
    if value == MILEAGE_SOURCE_MANUAL:
        return 5
    if value == MILEAGE_SOURCE_OWNTRACKS_PATH:
        return 4
    if value == MILEAGE_SOURCE_WAYPOINT_DISTANCE:
        return 2
    if value == MILEAGE_SOURCE_ESTIMATED_ODOMETER:
        return 1
    return 0


def _calculation_improves_existing_trip(
    existing_trip: Trip,
    calculation: MileageCalculation,
) -> bool:
    existing_rank = _mileage_source_rank(existing_trip.mileage_source)
    new_rank = _mileage_source_rank(calculation.mileage_source)
    if new_rank > existing_rank:
        return True
    if (
        new_rank == existing_rank
        and calculation.mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH
        and existing_trip.miles != calculation.miles
    ):
        return True
    if new_rank == existing_rank and _new_odometer_details_available(existing_trip, calculation):
        return True
    return False


def _trip_generation_key(
    origin_site_id: int | None,
    destination_site_id: int | None,
    started_at: datetime,
    ended_at: datetime,
) -> TripGenerationKey | None:
    if origin_site_id is None or destination_site_id is None:
        return None
    return (origin_site_id, destination_site_id, started_at, ended_at)


def _trip_recorded_values_key(
    trip_date: date,
    origin_site_id: int | None,
    destination_site_id: int | None,
    miles: Decimal,
    start_odometer_miles: Decimal | None,
    end_odometer_miles: Decimal | None,
) -> TripRecordedValuesKey | None:
    """Return the application key matching the recorded-value unique index."""

    if (
        origin_site_id is None
        or destination_site_id is None
        or start_odometer_miles is None
        or end_odometer_miles is None
    ):
        return None
    return (
        trip_date,
        origin_site_id,
        destination_site_id,
        Decimal(miles),
        Decimal(start_odometer_miles),
        Decimal(end_odometer_miles),
    )


def _trip_recorded_values_key_for_trip(trip: Trip) -> TripRecordedValuesKey | None:
    """Return one stored automatic trip's nonblank recorded-value signature."""

    return _trip_recorded_values_key(
        trip.trip_date,
        trip.origin_site_id,
        trip.destination_site_id,
        trip.miles,
        trip.start_odometer_miles,
        trip.end_odometer_miles,
    )


def _apply_trip_values(
    trip: Trip,
    *,
    trip_date: date,
    origin: Site,
    destination: Site,
    started_at: datetime,
    ended_at: datetime,
    calculation: MileageCalculation,
    notes: str,
) -> None:
    trip.trip_date = trip_date
    trip.origin_site_id = origin.id
    trip.destination_site_id = destination.id
    trip.started_at = started_at
    trip.ended_at = ended_at
    trip.start_latitude = _site_latitude(origin)
    trip.start_longitude = _site_longitude(origin)
    trip.end_latitude = _site_latitude(destination)
    trip.end_longitude = _site_longitude(destination)
    trip.origin_name = origin.name
    trip.destination_name = destination.name
    trip.miles = calculation.miles
    trip.start_odometer_miles = calculation.start_odometer_miles
    trip.end_odometer_miles = calculation.end_odometer_miles
    trip.start_odometer_source = calculation.start_odometer_source
    trip.end_odometer_source = calculation.end_odometer_source
    trip.mileage_source = calculation.mileage_source
    trip.source = AUTO_TRIP_SOURCE
    trip.notes = notes


def _add_or_update_trip(
    db: Session,
    generated: list[Trip],
    preserved_trips: list[Trip],
    existing_auto_trips: dict[TripGenerationKey, Trip],
    existing_auto_trips_by_recorded_values: dict[TripRecordedValuesKey, Trip],
    deleted_trip_keys: set[TripGenerationKey],
    *,
    origin: Site,
    destination: Site,
    started_at: datetime,
    ended_at: datetime,
    inferred_leave: bool,
    path_miles: Decimal | None,
    start_odometer: OdometerReading | None,
    end_odometer: OdometerReading | None,
    odometer_anchor_miles: Decimal | None,
    odometer_anchor_source: str | None,
) -> Trip | None:
    if _is_home_to_home(origin, destination):
        trip_logger.debug(
            "trip skipped reason=home_to_home origin=%s destination=%s started_at=%s ended_at=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
        )
        return None

    trip_date = datetime_to_local_date(started_at)
    if _overlaps_preserved_trip(
        preserved_trips,
        trip_date=trip_date,
        started_at=started_at,
        ended_at=ended_at,
    ):
        trip_logger.info(
            "trip skipped reason=manual_overlap origin=%s destination=%s trip_date=%s",
            origin.name,
            destination.name,
            trip_date.isoformat(),
        )
        return None

    trip_key = (origin.id, destination.id, started_at, ended_at)
    if trip_key in deleted_trip_keys:
        trip_logger.info(
            "trip skipped reason=user_deleted origin=%s destination=%s started_at=%s ended_at=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
        )
        return None

    existing_auto_trip = existing_auto_trips.get(trip_key)
    calculation = _mileage_calculation(
        origin,
        destination,
        path_miles=path_miles,
        start_odometer=start_odometer,
        end_odometer=end_odometer,
        odometer_anchor_miles=odometer_anchor_miles,
        odometer_anchor_source=odometer_anchor_source,
    )
    notes = _trip_notes(inferred_leave=inferred_leave)

    if _is_same_waypoint_under_minimum_miles(origin, destination, calculation.miles):
        if existing_auto_trip is not None:
            existing_recorded_values_key = _trip_recorded_values_key_for_trip(existing_auto_trip)
            if (
                existing_recorded_values_key is not None
                and existing_auto_trips_by_recorded_values.get(existing_recorded_values_key)
                is existing_auto_trip
            ):
                existing_auto_trips_by_recorded_values.pop(existing_recorded_values_key)
            suppress_trip_generation_for_deleted_trip(
                db,
                existing_auto_trip,
                reason="invalid_same_waypoint_under_one_mile",
            )
            db.delete(existing_auto_trip)
            existing_auto_trips.pop(trip_key, None)
            trip_logger.info(
                "trip removed reason=invalid_same_waypoint_under_one_mile "
                "origin=%s destination=%s miles=%s started_at=%s ended_at=%s",
                origin.name,
                destination.name,
                calculation.miles,
                started_at.isoformat(),
                ended_at.isoformat(),
            )
        else:
            trip_logger.debug(
                "trip skipped reason=invalid_same_waypoint_under_one_mile "
                "origin=%s destination=%s miles=%s started_at=%s ended_at=%s",
                origin.name,
                destination.name,
                calculation.miles,
                started_at.isoformat(),
                ended_at.isoformat(),
            )
        return None

    recorded_values_key = _trip_recorded_values_key(
        trip_date,
        origin.id,
        destination.id,
        calculation.miles,
        calculation.start_odometer_miles,
        calculation.end_odometer_miles,
    )
    recorded_values_trip = (
        existing_auto_trips_by_recorded_values.get(recorded_values_key)
        if recorded_values_key is not None
        else None
    )
    if recorded_values_trip is not None and recorded_values_trip is not existing_auto_trip:
        trip_logger.info(
            "trip skipped reason=duplicate_recorded_values existing_trip_id=%s "
            "origin=%s destination=%s trip_date=%s miles=%s "
            "start_odometer=%s end_odometer=%s started_at=%s ended_at=%s",
            recorded_values_trip.id,
            origin.name,
            destination.name,
            trip_date.isoformat(),
            calculation.miles,
            calculation.start_odometer_miles,
            calculation.end_odometer_miles,
            started_at.isoformat(),
            ended_at.isoformat(),
        )
        return recorded_values_trip

    if existing_auto_trip is not None and not _calculation_improves_existing_trip(
        existing_auto_trip,
        calculation,
    ):
        trip_logger.debug(
            "trip unchanged origin=%s destination=%s started_at=%s ended_at=%s source=%s",
            origin.name,
            destination.name,
            started_at.isoformat(),
            ended_at.isoformat(),
            existing_auto_trip.mileage_source,
        )
        return existing_auto_trip

    if calculation.mileage_source == MILEAGE_SOURCE_OWNTRACKS_PATH:
        notes = _append_note(notes, "Used OwnTracks location path between waypoint events.")
    elif calculation.mileage_source == MILEAGE_SOURCE_WAYPOINT_DISTANCE:
        notes = _append_note(
            notes,
            "Used waypoint distance because OwnTracks path data was unavailable.",
        )

    if existing_auto_trip is None and _should_skip_for_minimum_miles(
        origin,
        destination,
        calculation.miles,
    ):
        trip_logger.debug(
            "trip skipped reason=below_minimum origin=%s destination=%s miles=%s",
            origin.name,
            destination.name,
            calculation.miles,
        )
        return None

    if existing_auto_trip is None:
        trip = Trip()
        db.add(trip)
        action = "created"
    else:
        trip = existing_auto_trip
        action = "updated"

    previous_recorded_values_key = _trip_recorded_values_key_for_trip(trip)
    _apply_trip_values(
        trip,
        trip_date=trip_date,
        origin=origin,
        destination=destination,
        started_at=started_at,
        ended_at=ended_at,
        calculation=calculation,
        notes=notes,
    )
    if (
        previous_recorded_values_key is not None
        and previous_recorded_values_key != recorded_values_key
        and existing_auto_trips_by_recorded_values.get(previous_recorded_values_key) is trip
    ):
        existing_auto_trips_by_recorded_values.pop(previous_recorded_values_key)
    if recorded_values_key is not None:
        existing_auto_trips_by_recorded_values[recorded_values_key] = trip
    existing_auto_trips[trip_key] = trip
    generated.append(trip)
    trip_logger.info(
        "trip %s date=%s origin=%s destination=%s miles=%s source=%s "
        "start_odometer=%s start_odometer_source=%s end_odometer=%s "
        "end_odometer_source=%s inferred_leave=%s started_at=%s ended_at=%s",
        action,
        trip_date.isoformat(),
        origin.name,
        destination.name,
        calculation.miles,
        calculation.mileage_source,
        calculation.start_odometer_miles,
        calculation.start_odometer_source,
        calculation.end_odometer_miles,
        calculation.end_odometer_source,
        inferred_leave,
        started_at.isoformat(),
        ended_at.isoformat(),
    )
    return trip


def _existing_auto_trips_for_dates(
    db: Session,
    source_dates: list[date],
) -> tuple[
    dict[TripGenerationKey, Trip],
    dict[TripRecordedValuesKey, Trip],
]:
    trips = list(
        db.scalars(
            select(Trip)
            .where(Trip.source == AUTO_TRIP_SOURCE)
            .where(Trip.trip_date.in_(source_dates))
            .order_by(Trip.id.asc())
        )
    )
    by_generation = {
        (trip.origin_site_id, trip.destination_site_id, trip.started_at, trip.ended_at): trip
        for trip in trips
        if trip.origin_site_id is not None and trip.destination_site_id is not None
    }
    by_recorded_values: dict[TripRecordedValuesKey, Trip] = {}
    for trip in trips:
        recorded_values_key = _trip_recorded_values_key_for_trip(trip)
        if recorded_values_key is not None:
            # The database index already enforces uniqueness. setdefault also preserves the
            # oldest row if this helper is used while repairing a legacy inconsistent database.
            by_recorded_values.setdefault(recorded_values_key, trip)
    return by_generation, by_recorded_values


def _deleted_trip_keys_for_dates(db: Session, source_dates: list[date]) -> set[TripGenerationKey]:
    deleted_trips = list(
        db.scalars(
            select(DeletedTrip)
            .where(DeletedTrip.trip_date.in_(source_dates))
            .where(DeletedTrip.origin_site_id.is_not(None))
            .where(DeletedTrip.destination_site_id.is_not(None))
        )
    )
    return {
        (
            deleted_trip.origin_site_id,
            deleted_trip.destination_site_id,
            deleted_trip.started_at,
            deleted_trip.ended_at,
        )
        for deleted_trip in deleted_trips
        if deleted_trip.origin_site_id is not None and deleted_trip.destination_site_id is not None
    }


def _update_last_visited_from_confirmed_transitions(transitions: list[WaypointTransition]) -> None:
    """Persist waypoint last-visited timestamps only after dwell-confirmed enter events."""

    for transition in transitions:
        if transition.event != "enter":
            continue
        site = transition.site
        captured_at = transition.location.captured_at
        if site.last_visited_at is None or datetime_to_utc(captured_at) > datetime_to_utc(
            site.last_visited_at
        ):
            site.last_visited_at = captured_at


def _latest_odometer_reading_before(
    db: Session,
    before_datetime: datetime,
) -> TimedOdometerReading | None:
    """Return the latest master rolling odometer checkpoint before a trip starts."""

    checkpoint = db.scalar(
        select(TripProcessingCheckpoint)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .where(TripProcessingCheckpoint.odometer_anchor_miles.is_not(None))
        .where(TripProcessingCheckpoint.odometer_anchor_recorded_at <= before_datetime)
        .order_by(TripProcessingCheckpoint.updated_at.desc(), TripProcessingCheckpoint.id.desc())
        .limit(1)
    )
    if checkpoint is not None and checkpoint.odometer_anchor_miles is not None:
        checkpoint_recorded_at = (
            datetime_to_utc(checkpoint.odometer_anchor_recorded_at)
            if checkpoint.odometer_anchor_recorded_at is not None
            else datetime.min.replace(tzinfo=UTC)
        )
        return TimedOdometerReading(
            miles=Decimal(checkpoint.odometer_anchor_miles).quantize(
                ODOMETER_PRECISION,
                rounding=ROUND_HALF_UP,
            ),
            source=ODOMETER_SOURCE_OWNTRACKS_ROLLING,
            recorded_at=checkpoint_recorded_at,
            order_id=checkpoint.id or 0,
        )

    return None


def _owntracks_path_miles_between_datetimes(
    db: Session,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> Decimal | None:
    """Return OwnTracks movement distance between two UTC datetimes when rows are retained."""

    start_utc = datetime_to_utc(start_dt)
    end_utc = datetime_to_utc(end_dt)
    if end_utc <= start_utc:
        return Decimal("0.0")

    locations = list(
        db.scalars(
            select(OwnTracksLocation)
            .where(OwnTracksLocation.captured_at >= start_utc)
            .where(OwnTracksLocation.captured_at <= end_utc)
            .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
        )
    )
    if len(locations) < 2:
        return None

    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    total_miles = Decimal("0.0")
    previous_location = locations[0]
    for location in locations[1:]:
        total_miles += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location

    return total_miles.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _latest_checkpoint_reading(db: Session) -> TimedOdometerReading | None:
    """Return the current rolling OwnTracks odometer checkpoint when it exists."""

    checkpoint = _latest_checkpoint(db)
    if (
        checkpoint is None
        or checkpoint.odometer_anchor_miles is None
        or checkpoint.odometer_anchor_recorded_at is None
    ):
        return None
    return TimedOdometerReading(
        miles=Decimal(checkpoint.odometer_anchor_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        ),
        source=ODOMETER_SOURCE_OWNTRACKS_ROLLING,
        recorded_at=datetime_to_utc(checkpoint.odometer_anchor_recorded_at),
        order_id=checkpoint.id or 0,
    )


def _later_checkpoint_odometer_for_trip_start(
    db: Session,
    started_at: datetime,
) -> TimedOdometerReading | None:
    """Estimate a trip-start odometer from a later master checkpoint and retained path rows."""

    checkpoint = _latest_checkpoint_reading(db)
    if checkpoint is None or checkpoint.recorded_at <= datetime_to_utc(started_at):
        return None

    miles_after_start = _owntracks_path_miles_between_datetimes(
        db,
        start_dt=started_at,
        end_dt=checkpoint.recorded_at,
    )
    if miles_after_start is None:
        return None

    estimated_start = max(checkpoint.miles - miles_after_start, Decimal("0.0")).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    return TimedOdometerReading(
        miles=estimated_start,
        source=ODOMETER_SOURCE_OWNTRACKS_ROLLING,
        recorded_at=datetime_to_utc(started_at),
        order_id=checkpoint.order_id,
    )


def _odometer_anchor_for_trip_start(
    db: Session,
    started_at: datetime,
) -> TimedOdometerReading | None:
    """Return the best master rolling odometer reading available for a trip start."""

    prior_reading = _latest_odometer_reading_before(db, started_at)
    if prior_reading is not None:
        return prior_reading
    return _later_checkpoint_odometer_for_trip_start(db, started_at)


def _latest_odometer_before(db: Session, before_datetime: datetime) -> Decimal | None:
    """Return the latest master rolling odometer before a trip starts."""

    reading = _odometer_anchor_for_trip_start(db, before_datetime)
    return reading.miles if reading is not None else None


def _latest_trip_before(db: Session, before_datetime: datetime) -> Trip | None:
    """Return the latest trip ending before a datetime in chronological order."""

    return db.scalar(
        select(Trip)
        .where(Trip.ended_at < before_datetime)
        .where(Trip.end_odometer_miles.is_not(None))
        .order_by(Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )


def _latest_current_odometer(db: Session) -> Decimal | None:
    """Return the current master rolling OwnTracks odometer checkpoint."""

    return _latest_checkpoint_odometer(db)


def _latest_checkpoint_odometer(db: Session) -> Decimal | None:
    """Return the current rolling OwnTracks odometer checkpoint when available."""

    checkpoint = _latest_checkpoint_reading(db)
    if checkpoint is None:
        return None
    return checkpoint.miles


def _month_date_bounds(year: int, month: int) -> tuple[date, date]:
    """Return inclusive start and exclusive end dates for one local report month."""

    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start_date, end_date


def _month_trips_ordered(db: Session, year: int, month: int) -> list[Trip]:
    """Load month trips in chronological odometer order."""

    start_date, end_date = _month_date_bounds(year, month)
    return list(
        db.scalars(
            select(Trip)
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date < end_date)
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )


def _trips_from_trip_ordered(db: Session, first_trip: Trip) -> list[Trip]:
    """Load one trip and every chronologically later trip."""

    if first_trip.id is None:
        return []
    return list(
        db.scalars(
            select(Trip)
            .where(
                or_(
                    Trip.started_at > first_trip.started_at,
                    and_(Trip.started_at == first_trip.started_at, Trip.id >= first_trip.id),
                )
            )
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )


def _all_trips_ordered(db: Session) -> list[Trip]:
    """Load every trip in chronological odometer order."""

    return list(
        db.scalars(
            select(Trip).order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )


def _latest_checkpoint(db: Session) -> TripProcessingCheckpoint | None:
    """Return the rolling odometer checkpoint when it already exists."""

    return db.scalar(
        select(TripProcessingCheckpoint)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .limit(1)
    )


def _month_resequence_anchor(db: Session, first_trip: Trip) -> Decimal:
    """Choose the stable odometer start point for resequencing a month."""

    if first_trip.start_odometer_miles is not None:
        return Decimal(first_trip.start_odometer_miles).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )

    prior_odometer = _latest_odometer_before(db, first_trip.started_at)
    if prior_odometer is not None:
        return Decimal(prior_odometer).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
    return Decimal("0.0")


def _positive_odometer_gap(
    start_odometer_miles: Decimal | None,
    previous_end_odometer_miles: Decimal | None,
) -> Decimal:
    """Return an existing positive non-trip odometer gap between two trip rows."""

    if start_odometer_miles is None or previous_end_odometer_miles is None:
        return Decimal("0.0")
    gap = Decimal(start_odometer_miles) - Decimal(previous_end_odometer_miles)
    if gap <= 0:
        return Decimal("0.0")
    return gap.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)


def _odometer_gaps_for_ordered_trips(
    ordered_trips: list[Trip],
    *,
    previous_end_odometer_miles: Decimal | None = None,
) -> list[Decimal]:
    """Capture existing positive gaps before resequencing mutates trip odometers."""

    gaps: list[Decimal] = []
    previous_end = previous_end_odometer_miles
    for trip in ordered_trips:
        gaps.append(_positive_odometer_gap(trip.start_odometer_miles, previous_end))
        if trip.end_odometer_miles is not None:
            previous_end = trip.end_odometer_miles
    return gaps


def _resequence_start_source(
    *,
    index: int,
    first_start_source: str,
    odometer_gap: Decimal,
) -> str:
    """Return the display source for a resequenced trip start odometer."""

    if odometer_gap > 0:
        return ODOMETER_SOURCE_OWNTRACKS_ROLLING
    if index == 0:
        return first_start_source
    return ODOMETER_SOURCE_PREVIOUS_TRIP


def resequence_all_trip_odometers_to_manual_reading(
    db: Session,
    odometer_miles: Decimal,
) -> int:
    """Align all trip odometers so the latest trip ends at a manual Home reading.

    The calculation works backward from the exact reading. Each stored trip distance and every
    existing positive between-trip odometer gap are preserved. The master rolling checkpoint is
    intentionally not changed here; its explicit manual update remains the caller's responsibility.
    """

    ordered_trips = _all_trips_ordered(db)
    if not ordered_trips:
        return 0

    target_end_odometer = Decimal(odometer_miles).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    odometer_gaps = _odometer_gaps_for_ordered_trips(ordered_trips)
    adjusted_values: list[tuple[Decimal, Decimal]] = [
        (Decimal("0.0"), Decimal("0.0")) for _trip in ordered_trips
    ]
    current_end_odometer = target_end_odometer

    for index in range(len(ordered_trips) - 1, -1, -1):
        trip = ordered_trips[index]
        trip_distance = Decimal(trip.miles).quantize(
            DISTANCE_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        start_odometer = (current_end_odometer - trip_distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        if start_odometer < 0:
            raise TripOdometerAdjustmentError(
                "The manual odometer reading is too low to preserve all saved trip miles and gaps."
            )

        adjusted_values[index] = (start_odometer, current_end_odometer)
        if index > 0:
            previous_end_odometer = (start_odometer - odometer_gaps[index]).quantize(
                ODOMETER_PRECISION,
                rounding=ROUND_HALF_UP,
            )
            if previous_end_odometer < 0:
                raise TripOdometerAdjustmentError(
                    "The manual odometer reading is too low to preserve all saved trip miles "
                    "and gaps."
                )
            current_end_odometer = previous_end_odometer

    final_index = len(ordered_trips) - 1
    for index, trip in enumerate(ordered_trips):
        start_odometer, end_odometer = adjusted_values[index]
        trip.start_odometer_miles = start_odometer
        trip.end_odometer_miles = end_odometer
        trip.start_odometer_source = _resequence_start_source(
            index=index,
            first_start_source=trip.start_odometer_source or ODOMETER_SOURCE_ESTIMATED,
            odometer_gap=odometer_gaps[index],
        )
        trip.end_odometer_source = (
            ODOMETER_SOURCE_MANUAL if index == final_index else ODOMETER_SOURCE_ESTIMATED
        )

    trip_logger.info(
        "Aligned all trip odometers to manual Home reading trips=%s "
        "start_odometer=%s end_odometer=%s",
        len(ordered_trips),
        ordered_trips[0].start_odometer_miles,
        ordered_trips[-1].end_odometer_miles,
    )
    return len(ordered_trips)


def resequence_month_trip_odometers(db: Session, year: int, month: int) -> int:
    """Recalculate every trip odometer in a month from ordered trip distances."""

    month_trips = _month_trips_ordered(db, year, month)
    if not month_trips:
        return 0

    current_odometer = _month_resequence_anchor(db, month_trips[0])
    prior_trip = _latest_trip_before(db, month_trips[0].started_at)
    odometer_gaps = _odometer_gaps_for_ordered_trips(
        month_trips,
        previous_end_odometer_miles=(
            prior_trip.end_odometer_miles if prior_trip is not None else None
        ),
    )
    for index, trip in enumerate(month_trips):
        current_odometer = (current_odometer + odometer_gaps[index]).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip_distance = Decimal(trip.miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        start_odometer = current_odometer.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
        end_odometer = (start_odometer + trip_distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip.start_odometer_miles = start_odometer
        trip.end_odometer_miles = end_odometer
        trip.start_odometer_source = _resequence_start_source(
            index=index,
            first_start_source=ODOMETER_SOURCE_PREVIOUS_TRIP,
            odometer_gap=odometer_gaps[index],
        )
        trip.end_odometer_source = ODOMETER_SOURCE_ESTIMATED
        current_odometer = end_odometer

    trip_logger.info(
        "Resequenced trip odometers year=%s month=%s trips=%s start_odometer=%s end_odometer=%s",
        year,
        month,
        len(month_trips),
        month_trips[0].start_odometer_miles,
        month_trips[-1].end_odometer_miles,
    )
    return len(month_trips)


def resequence_month_trip_odometers_from(db: Session, first_trip: Trip) -> int:
    """Recalculate an edited trip and later trips in its start month only.

    The edited trip keeps its existing start odometer. Existing positive non-trip gaps after that
    row are captured before any values are changed, then reapplied to the later rows. Trips before
    the edited row and trips whose start date belongs to another month are never modified.
    """

    db.flush()
    month_trips = _month_trips_ordered(
        db,
        first_trip.trip_date.year,
        first_trip.trip_date.month,
    )
    ordered_trips = [
        trip
        for trip in month_trips
        if (
            trip.started_at > first_trip.started_at
            or (trip.started_at == first_trip.started_at and trip.id >= first_trip.id)
        )
    ]
    if not ordered_trips:
        return 0

    current_odometer = first_trip.start_odometer_miles
    if current_odometer is None:
        previous_trip = _latest_trip_before(db, first_trip.started_at)
        if previous_trip is not None and previous_trip.end_odometer_miles is not None:
            current_odometer = previous_trip.end_odometer_miles
        else:
            current_odometer = _month_resequence_anchor(db, first_trip)
    current_odometer = Decimal(current_odometer).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    odometer_gaps = _odometer_gaps_for_ordered_trips(ordered_trips)
    first_start_source = ODOMETER_SOURCE_PREVIOUS_TRIP

    for index, trip in enumerate(ordered_trips):
        if index > 0:
            current_odometer = (current_odometer + odometer_gaps[index]).quantize(
                ODOMETER_PRECISION,
                rounding=ROUND_HALF_UP,
            )
        trip_distance = Decimal(trip.miles).quantize(
            DISTANCE_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip.start_odometer_miles = current_odometer
        trip.end_odometer_miles = (current_odometer + trip_distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip.start_odometer_source = _resequence_start_source(
            index=index,
            first_start_source=first_start_source,
            odometer_gap=odometer_gaps[index] if index > 0 else Decimal("0.0"),
        )
        trip.end_odometer_source = ODOMETER_SOURCE_ESTIMATED
        current_odometer = trip.end_odometer_miles

    trip_logger.info(
        "Resequenced edited trip and later same-month odometers from_trip_id=%s trips=%s "
        "start_odometer=%s end_odometer=%s",
        first_trip.id,
        len(ordered_trips),
        ordered_trips[0].start_odometer_miles,
        ordered_trips[-1].end_odometer_miles,
    )
    return len(ordered_trips)


def _reconstructed_non_trip_gap(
    db: Session,
    previous_trip: Trip,
    current_trip: Trip,
) -> Decimal | None:
    """Reconstruct one between-trip gap from retained OwnTracks history when possible."""

    if current_trip.started_at <= previous_trip.ended_at:
        return Decimal("0.0")
    locations = list(
        db.scalars(
            select(OwnTracksLocation)
            .where(OwnTracksLocation.captured_at >= previous_trip.ended_at)
            .where(OwnTracksLocation.captured_at <= current_trip.started_at)
            .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
        )
    )
    if not locations:
        return None
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    reconstructed_gap = _path_distance_miles(
        locations,
        sites,
        sites_by_name,
        sites_by_region_id,
    )
    if reconstructed_gap is None:
        return None
    return reconstructed_gap.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)


def emergency_rebuild_trip_odometers_to_manual_reading(
    db: Session,
    odometer_miles: Decimal,
) -> EmergencyOdometerRebuildResult:
    """Best-effort rebuild every trip odometer against one trusted current reading.

    Stored trip distances are immutable in this operation. Positive gaps below 200 miles are
    preserved. Gaps of 200 miles or more are first reconstructed from retained OwnTracks history;
    when that is unavailable or still 200 miles or more, the gap is discarded. If the remaining
    gaps would push the oldest reconstructed odometer below zero, the largest remaining gaps are
    discarded until the sequence fits.
    """

    ordered_trips = _all_trips_ordered(db)
    if not ordered_trips:
        return EmergencyOdometerRebuildResult(0, 0, 0, 0)

    target_end_odometer = Decimal(odometer_miles).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    trip_distances = [
        Decimal(trip.miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        for trip in ordered_trips
    ]
    total_trip_distance = sum(trip_distances, Decimal("0.0"))
    if total_trip_distance > target_end_odometer:
        raise TripOdometerAdjustmentError(
            "The manual odometer reading is lower than the total saved trip distance; "
            "trip distances were not changed."
        )

    original_gaps = _odometer_gaps_for_ordered_trips(ordered_trips)
    selected_gaps = list(original_gaps)
    reconstructed_gap_indexes: set[int] = set()
    discarded_gap_indexes: set[int] = set()
    for index in range(1, len(ordered_trips)):
        if selected_gaps[index] < EMERGENCY_MAX_PRESERVED_GAP_MILES:
            continue
        reconstructed_gap = _reconstructed_non_trip_gap(
            db,
            ordered_trips[index - 1],
            ordered_trips[index],
        )
        if (
            reconstructed_gap is not None
            and reconstructed_gap < EMERGENCY_MAX_PRESERVED_GAP_MILES
        ):
            selected_gaps[index] = reconstructed_gap
            reconstructed_gap_indexes.add(index)
        else:
            selected_gaps[index] = Decimal("0.0")
            discarded_gap_indexes.add(index)

    required_distance = total_trip_distance + sum(selected_gaps, Decimal("0.0"))
    if required_distance > target_end_odometer:
        for index in sorted(
            range(1, len(selected_gaps)),
            key=lambda gap_index: selected_gaps[gap_index],
            reverse=True,
        ):
            if required_distance <= target_end_odometer:
                break
            if selected_gaps[index] <= 0:
                continue
            required_distance -= selected_gaps[index]
            selected_gaps[index] = Decimal("0.0")
            discarded_gap_indexes.add(index)
            reconstructed_gap_indexes.discard(index)

    adjusted_values: list[tuple[Decimal, Decimal]] = [
        (Decimal("0.0"), Decimal("0.0")) for _trip in ordered_trips
    ]
    current_end_odometer = target_end_odometer
    for index in range(len(ordered_trips) - 1, -1, -1):
        start_odometer = (current_end_odometer - trip_distances[index]).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        adjusted_values[index] = (start_odometer, current_end_odometer)
        if index > 0:
            current_end_odometer = (start_odometer - selected_gaps[index]).quantize(
                ODOMETER_PRECISION,
                rounding=ROUND_HALF_UP,
            )

    final_index = len(ordered_trips) - 1
    for index, trip in enumerate(ordered_trips):
        start_odometer, end_odometer = adjusted_values[index]
        trip.start_odometer_miles = start_odometer
        trip.end_odometer_miles = end_odometer
        trip.start_odometer_source = _resequence_start_source(
            index=index,
            first_start_source=trip.start_odometer_source or ODOMETER_SOURCE_ESTIMATED,
            odometer_gap=selected_gaps[index],
        )
        trip.end_odometer_source = (
            ODOMETER_SOURCE_MANUAL if index == final_index else ODOMETER_SOURCE_ESTIMATED
        )

    preserved_gap_count = sum(
        1
        for index, gap in enumerate(selected_gaps)
        if index > 0 and gap > 0 and index not in reconstructed_gap_indexes
    )
    result = EmergencyOdometerRebuildResult(
        adjusted_trip_count=len(ordered_trips),
        preserved_gap_count=preserved_gap_count,
        reconstructed_gap_count=len(reconstructed_gap_indexes),
        discarded_gap_count=len(discarded_gap_indexes),
    )
    trip_logger.warning(
        "Emergency rebuilt trip odometers trips=%s preserved_gaps=%s reconstructed_gaps=%s "
        "discarded_gaps=%s end_odometer=%s",
        result.adjusted_trip_count,
        result.preserved_gap_count,
        result.reconstructed_gap_count,
        result.discarded_gap_count,
        target_end_odometer,
    )
    return result


def _manual_trip_start_odometer(db: Session, trip: Trip) -> tuple[Decimal, str]:
    """Return the starting odometer and source for a newly added manual trip."""

    checkpoint_odometer = _latest_checkpoint_odometer(db)
    if checkpoint_odometer is not None:
        return (
            Decimal(checkpoint_odometer).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP),
            ODOMETER_SOURCE_OWNTRACKS_ROLLING,
        )

    prior_timed_odometer = _latest_odometer_before(db, trip.started_at)
    if prior_timed_odometer is not None:
        return (
            Decimal(prior_timed_odometer).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP),
            ODOMETER_SOURCE_PREVIOUS_TRIP,
        )

    current_odometer = _latest_current_odometer(db)
    if current_odometer is not None:
        return (
            Decimal(current_odometer).quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP),
            ODOMETER_SOURCE_MANUAL,
        )

    return Decimal("0.0"), ODOMETER_SOURCE_MANUAL


def resequence_trip_odometers_from(
    db: Session,
    first_trip: Trip,
    *,
    start_odometer_miles: Decimal | None = None,
    start_odometer_source: str | None = None,
    previous_trip: Trip | None = None,
) -> int:
    """Recalculate one trip and every later trip from ordered trip distances."""

    db.flush()
    ordered_trips = _trips_from_trip_ordered(db, first_trip)
    if not ordered_trips:
        return 0

    if start_odometer_miles is None:
        start_odometer_miles = first_trip.start_odometer_miles
    if start_odometer_miles is None:
        start_odometer_miles, start_odometer_source = _manual_trip_start_odometer(db, first_trip)

    current_odometer = Decimal(start_odometer_miles).quantize(
        ODOMETER_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    first_start_source = (
        start_odometer_source
        or first_trip.start_odometer_source
        or ODOMETER_SOURCE_PREVIOUS_TRIP
    )
    odometer_gaps = _odometer_gaps_for_ordered_trips(
        ordered_trips,
        previous_end_odometer_miles=(
            previous_trip.end_odometer_miles if previous_trip is not None else None
        ),
    )

    for index, trip in enumerate(ordered_trips):
        current_odometer = (current_odometer + odometer_gaps[index]).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip_distance = Decimal(trip.miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        start_odometer = current_odometer.quantize(ODOMETER_PRECISION, rounding=ROUND_HALF_UP)
        end_odometer = (start_odometer + trip_distance).quantize(
            ODOMETER_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        trip.start_odometer_miles = start_odometer
        trip.end_odometer_miles = end_odometer
        trip.start_odometer_source = _resequence_start_source(
            index=index,
            first_start_source=first_start_source,
            odometer_gap=odometer_gaps[index],
        )
        trip.end_odometer_source = ODOMETER_SOURCE_ESTIMATED
        current_odometer = end_odometer

    trip_logger.info(
        "Resequenced future trip odometers from_trip_id=%s trips=%s "
        "start_odometer=%s end_odometer=%s",
        first_trip.id,
        len(ordered_trips),
        ordered_trips[0].start_odometer_miles,
        ordered_trips[-1].end_odometer_miles,
    )
    return len(ordered_trips)


def backfill_missing_trip_odometers(db: Session) -> int:
    """Fill blank trip odometer fields from the master OwnTracks odometer when possible."""

    trips = list(
        db.scalars(
            select(Trip)
            .where(
                or_(
                    Trip.start_odometer_miles.is_(None),
                    Trip.end_odometer_miles.is_(None),
                )
            )
            .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
        )
    )
    updated_count = 0
    for trip in trips:
        odometer_anchor = _odometer_anchor_for_trip_start(db, trip.started_at)
        calculation = _estimated_odometer_calculation(
            Decimal(trip.miles),
            mileage_source=trip.mileage_source,
            start_odometer_miles=(
                Decimal(trip.start_odometer_miles)
                if trip.start_odometer_miles is not None
                else None
            ),
            end_odometer_miles=(
                Decimal(trip.end_odometer_miles)
                if trip.end_odometer_miles is not None
                else None
            ),
            start_odometer_source=trip.start_odometer_source,
            end_odometer_source=trip.end_odometer_source,
            odometer_anchor_miles=(
                odometer_anchor.miles if odometer_anchor is not None else None
            ),
            odometer_anchor_source=(
                odometer_anchor.source if odometer_anchor is not None else None
            ),
        )
        if calculation is None:
            continue

        calculated_values = (
            calculation.start_odometer_miles,
            calculation.end_odometer_miles,
            calculation.start_odometer_source,
            calculation.end_odometer_source,
        )
        existing_values = (
            trip.start_odometer_miles,
            trip.end_odometer_miles,
            trip.start_odometer_source,
            trip.end_odometer_source,
        )
        if calculated_values == existing_values:
            continue

        trip.start_odometer_miles = calculation.start_odometer_miles
        trip.end_odometer_miles = calculation.end_odometer_miles
        trip.start_odometer_source = calculation.start_odometer_source
        trip.end_odometer_source = calculation.end_odometer_source
        updated_count += 1

    if updated_count:
        trip_logger.info("Backfilled missing trip odometers trips=%s", updated_count)
    return updated_count


def generate_trips(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    as_of: datetime | None = None,
) -> list[Trip]:
    generation_start_dt, generation_end_dt = _date_range_bounds(start_date, end_date)
    dwell_padding = timedelta(minutes=get_settings().owntracks_waypoint_dwell_minutes)
    sites = list(db.scalars(select(Site).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    locations = _locations_for_range(
        db,
        start_date,
        end_date,
        end_padding=TRIP_END_CROSS_DAY_LOOKAHEAD + dwell_padding,
    )
    if not locations:
        trip_logger.debug(
            "trip generation skipped reason=no_locations start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    all_transitions = _waypoint_transitions(locations, sites, as_of=as_of)
    transitions = [
        transition
        for transition in all_transitions
        if generation_start_dt
        <= datetime_to_utc(transition.location.captured_at)
        < generation_end_dt
    ]
    if transitions and transitions[-1].event == "leave":
        cross_day_arrival = next(
            (
                transition
                for transition in all_transitions
                if transition.event == "enter"
                and datetime_to_utc(transition.location.captured_at) >= generation_end_dt
                and datetime_to_utc(transition.location.captured_at)
                > datetime_to_utc(transitions[-1].location.captured_at)
            ),
            None,
        )
        if cross_day_arrival is not None:
            transitions.append(cross_day_arrival)
    if not transitions:
        trip_logger.debug(
            "trip generation skipped reason=no_waypoint_transitions start_date=%s end_date=%s",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return []

    _update_last_visited_from_confirmed_transitions(transitions)
    preserved_trips = _preserved_trips_for_range(db, start_date, end_date)
    source_dates = sorted(
        {datetime_to_local_date(event.location.captured_at) for event in transitions}
    )
    existing_auto_trips, existing_auto_trips_by_recorded_values = (
        _existing_auto_trips_for_dates(db, source_dates)
    )
    deleted_trip_keys = _deleted_trip_keys_for_dates(db, source_dates)
    home_site = _home_site(sites)
    pending_leave: WaypointTransition | None = None
    last_arrival: WaypointTransition | None = None
    ignored_leave_site_ids: set[int] = set()
    generated: list[Trip] = []

    for transition in transitions:
        if transition.event == "leave":
            _odometer_for_transition(db, transition)
            if (
                pending_leave is not None
                and pending_leave.site.id != transition.site.id
            ) or (
                last_arrival is not None
                and last_arrival.site.id != transition.site.id
            ):
                trip_logger.debug(
                    "waypoint leave skipped reason=no_confirmed_arrival site=%s "
                    "captured_at=%s pending_origin=%s last_arrival=%s",
                    transition.site.name,
                    transition.location.captured_at.isoformat(),
                    pending_leave.site.name if pending_leave is not None else "",
                    last_arrival.site.name if last_arrival is not None else "",
                )
                ignored_leave_site_ids.add(transition.site.id)
                continue
            pending_leave = transition
            continue

        end_odometer = _odometer_for_transition(db, transition)
        if pending_leave is not None:
            odometer_anchor = _odometer_anchor_for_trip_start(
                db,
                pending_leave.location.captured_at,
            )
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                existing_auto_trips_by_recorded_values,
                deleted_trip_keys,
                origin=pending_leave.site,
                destination=transition.site,
                started_at=pending_leave.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=False,
                path_miles=_trip_path_miles(
                    locations,
                    started_at=pending_leave.location.captured_at,
                    ended_at=transition.location.captured_at,
                    sites=sites,
                    sites_by_name=sites_by_name,
                    sites_by_region_id=sites_by_region_id,
                ),
                start_odometer=_odometer_for_transition(db, pending_leave),
                end_odometer=end_odometer,
                odometer_anchor_miles=(
                    odometer_anchor.miles if odometer_anchor is not None else None
                ),
                odometer_anchor_source=(
                    odometer_anchor.source if odometer_anchor is not None else None
                ),
            )
        elif (
            last_arrival is not None
            and last_arrival.site.id != transition.site.id
            and transition.site.id not in ignored_leave_site_ids
        ):
            odometer_anchor = _odometer_anchor_for_trip_start(db, transition.location.captured_at)
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                existing_auto_trips_by_recorded_values,
                deleted_trip_keys,
                origin=last_arrival.site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                path_miles=None,
                start_odometer=_odometer_for_transition(db, last_arrival),
                end_odometer=end_odometer,
                odometer_anchor_miles=(
                    odometer_anchor.miles if odometer_anchor is not None else None
                ),
                odometer_anchor_source=(
                    odometer_anchor.source if odometer_anchor is not None else None
                ),
            )
        elif (
            last_arrival is None
            and home_site is not None
            and home_site.id != transition.site.id
            and transition.site.id not in ignored_leave_site_ids
        ):
            odometer_anchor = _odometer_anchor_for_trip_start(db, transition.location.captured_at)
            trip = _add_or_update_trip(
                db,
                generated,
                preserved_trips,
                existing_auto_trips,
                existing_auto_trips_by_recorded_values,
                deleted_trip_keys,
                origin=home_site,
                destination=transition.site,
                started_at=transition.location.captured_at,
                ended_at=transition.location.captured_at,
                inferred_leave=True,
                path_miles=None,
                start_odometer=None,
                end_odometer=end_odometer,
                odometer_anchor_miles=(
                    odometer_anchor.miles if odometer_anchor is not None else None
                ),
                odometer_anchor_source=(
                    odometer_anchor.source if odometer_anchor is not None else None
                ),
            )
        else:
            if transition.site.id in ignored_leave_site_ids:
                trip_logger.debug(
                    "waypoint enter skipped reason=ignored_prior_leave site=%s "
                    "captured_at=%s",
                    transition.site.name,
                    transition.location.captured_at.isoformat(),
                )
            trip = None

        ignored_leave_site_ids.discard(transition.site.id)
        last_arrival = transition
        pending_leave = None

    db.commit()
    for trip in generated:
        db.refresh(trip)
    trip_logger.info(
        "trip generation complete start_date=%s end_date=%s transitions=%s generated=%s",
        start_date.isoformat(),
        end_date.isoformat(),
        len(transitions),
        len(generated),
    )
    return generated


def mark_trip_user_edited(trip: Trip) -> None:
    """Record user edits without changing how the trip was originally created."""

    if trip.source == AUTO_TRIP_SOURCE:
        trip.notes = _append_note(trip.notes, USER_EDITED_TRIP_NOTE)


def mark_trip_manually_reviewed(trip: Trip) -> None:
    """Compatibility wrapper for older callers that recorded row edits."""

    mark_trip_user_edited(trip)


def suppress_trip_generation_for_deleted_trip(
    db: Session,
    trip: Trip,
    *,
    reason: str = "user_deleted",
) -> DeletedTrip | None:
    """Save an exact source-event tombstone for one deleted automatic trip."""

    trip_key = _trip_generation_key(
        trip.origin_site_id,
        trip.destination_site_id,
        trip.started_at,
        trip.ended_at,
    )
    if trip_key is None:
        return None

    origin_site_id, destination_site_id, started_at, ended_at = trip_key
    deleted_trip = db.scalar(
        select(DeletedTrip)
        .where(DeletedTrip.origin_site_id == origin_site_id)
        .where(DeletedTrip.destination_site_id == destination_site_id)
        .where(DeletedTrip.started_at == started_at)
        .where(DeletedTrip.ended_at == ended_at)
    )
    if deleted_trip is None:
        deleted_trip = DeletedTrip(
            deleted_trip_id=trip.id,
            trip_date=trip.trip_date,
            origin_site_id=origin_site_id,
            destination_site_id=destination_site_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        db.add(deleted_trip)

    deleted_trip.origin_name = trip.origin_display_name
    deleted_trip.destination_name = trip.destination_display_name
    deleted_trip.miles = trip.miles
    deleted_trip.source = trip.source
    deleted_trip.mileage_source = trip.mileage_source
    deleted_trip.reason = reason
    deleted_trip.notes = trip.notes or ""
    return deleted_trip


def delete_trip(db: Session, trip: Trip) -> DeletedTrip | None:
    deleted_trip = suppress_trip_generation_for_deleted_trip(db, trip)
    db.delete(trip)
    return deleted_trip


def _manual_trip_day_start(trip_date: date) -> datetime:
    start_dt, _ = local_day_bounds(trip_date)
    return start_dt


def _manual_trip_insert_datetime(db: Session, trip_date: date) -> datetime:
    """Place a new manual trip after existing trips on the selected local date."""

    start_dt, end_dt = local_day_bounds(trip_date)
    latest_same_day_trip = db.scalar(
        select(Trip)
        .where(Trip.trip_date == trip_date)
        .order_by(Trip.started_at.desc(), Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )
    candidate_dt = datetime_to_utc(start_dt)
    current_local_dt = local_now()
    if trip_date == current_local_dt.date():
        candidate_dt = datetime_to_utc(current_local_dt)
    if latest_same_day_trip is not None:
        latest_trip_dt = max(
            datetime_to_utc(latest_same_day_trip.started_at),
            datetime_to_utc(latest_same_day_trip.ended_at),
        )
        candidate_dt = max(candidate_dt, latest_trip_dt + timedelta(seconds=1))

    end_limit = datetime_to_utc(end_dt) - timedelta(microseconds=1)
    if candidate_dt < datetime_to_utc(start_dt):
        return start_dt
    if candidate_dt > end_limit:
        return end_limit
    return candidate_dt


def create_manual_trip(
    db: Session,
    *,
    trip_date: date,
    origin_name: str,
    destination_name: str,
    miles: Decimal,
) -> Trip:
    started_at = _manual_trip_insert_datetime(db, trip_date)
    previous_trip = _latest_trip_before(db, started_at)
    trip = Trip(
        trip_date=trip_date,
        origin_site_id=None,
        destination_site_id=None,
        started_at=started_at,
        ended_at=started_at,
        start_latitude=Decimal("0.0000000"),
        start_longitude=Decimal("0.0000000"),
        end_latitude=Decimal("0.0000000"),
        end_longitude=Decimal("0.0000000"),
        origin_name=normalize_location_name(origin_name),
        destination_name=normalize_location_name(destination_name),
        miles=Decimal(miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP),
        mileage_source=MILEAGE_SOURCE_MANUAL,
        source=MANUAL_TRIP_SOURCE,
        notes=MANUAL_TRIP_NOTE,
    )
    db.add(trip)
    db.flush()
    start_odometer, start_source = _manual_trip_start_odometer(db, trip)
    resequence_trip_odometers_from(
        db,
        trip,
        start_odometer_miles=start_odometer,
        start_odometer_source=start_source,
        previous_trip=previous_trip,
    )
    return trip


def update_trip_details(
    trip: Trip,
    origin_name: str,
    destination_name: str,
    miles: Decimal | None = None,
    trip_date: date | None = None,
) -> None:
    if trip_date is not None:
        trip.trip_date = trip_date
        trip.started_at = _manual_trip_day_start(trip_date)
        trip.ended_at = trip.started_at
    trip.origin_name = normalize_location_name(origin_name)
    trip.destination_name = normalize_location_name(destination_name)
    if miles is not None:
        trip.miles = Decimal(miles).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
        trip.mileage_source = MILEAGE_SOURCE_MANUAL
    mark_trip_user_edited(trip)


def update_trip_location_names(trip: Trip, origin_name: str, destination_name: str) -> None:
    update_trip_details(trip, origin_name, destination_name)


def monthly_miles(db: Session, year: int, month: int) -> Decimal:
    start_date, end_date = _month_date_bounds(year, month)
    stmt = (
        select(Trip)
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
    )
    total = sum((trip.miles for trip in db.scalars(stmt)), Decimal("0.0"))
    return total.quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)
