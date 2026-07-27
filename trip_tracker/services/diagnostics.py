from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trip_tracker.config import get_settings
from trip_tracker.models import OwnTracksLocation, Site
from trip_tracker.services.mileage import (
    METERS_PER_MILE,
    haversine_miles,
    site_for_location,
)
from trip_tracker.services.timezone import datetime_to_utc


@dataclass(frozen=True)
class OwnTracksEntriesPage:
    entries: list[OwnTracksLocation]
    page: int
    page_size: int
    total: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def first_item(self) -> int:
        if self.total == 0:
            return 0
        return ((self.page - 1) * self.page_size) + 1

    @property
    def last_item(self) -> int:
        return min(self.page * self.page_size, self.total)


@dataclass(frozen=True)
class OwnTracksStateChange:
    """A single high-signal movement state transition for the Diagnostics page."""

    captured_at: datetime
    state: str
    label: str
    site_name: str | None = None
    duration_seconds: int | None = None
    source: str = "Location inference"
    received_delay_seconds: int | None = None
    odometer_miles: Decimal | None = None
    distance_miles: Decimal | None = None

    @property
    def duration_display(self) -> str:
        return _format_elapsed_seconds(self.duration_seconds)

    @property
    def received_delay_display(self) -> str:
        return _format_elapsed_seconds(self.received_delay_seconds)


@dataclass(frozen=True)
class CurrentOwnTracksState:
    """The latest inferred OwnTracks state shown at the top of the Diagnostics page."""

    state: str
    label: str
    site_name: str | None = None
    arrived_at: datetime | None = None
    detected_at: datetime | None = None
    distance_miles: Decimal | None = None


@dataclass(frozen=True)
class OwnTracksMovementDiagnostics:
    """Current OwnTracks state and the recent transition-only state log."""

    current_state: CurrentOwnTracksState
    state_changes: list[OwnTracksStateChange]


def paginated_owntracks_entries(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 20,
) -> OwnTracksEntriesPage:
    page_size = max(page_size, 1)
    total = db.scalar(select(func.count(OwnTracksLocation.id))) or 0
    total_pages = max(1, ceil(total / page_size))
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * page_size
    newest_entries = list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    entries = list(reversed(newest_entries))
    return OwnTracksEntriesPage(
        entries=entries,
        page=current_page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


def recent_owntracks_entries(db: Session, limit: int = 20) -> list[OwnTracksLocation]:
    return paginated_owntracks_entries(db, page=1, page_size=limit).entries


def owntracks_entry_event_label(location: OwnTracksLocation) -> str:
    """Return a readable Diagnostics event label for one stored OwnTracks row."""

    payload_type = _payload_type(location)
    if payload_type == "transition":
        transition_event = _transition_event(location)
        if transition_event == "enter":
            return "Waypoint enter"
        if transition_event == "leave":
            return "Waypoint leave"
        return "Waypoint event"
    if payload_type == "location":
        return "Location event"
    if payload_type == "waypoint":
        return "Waypoint event"
    return "OwnTracks event"


def owntracks_entry_received_delay_display(location: OwnTracksLocation) -> str:
    """Return the capture-to-receive delay for one stored OwnTracks row."""

    return _format_elapsed_seconds(_elapsed_seconds(location.captured_at, location.received_at))


def _payload_type(location: OwnTracksLocation) -> str:
    """Return the normalized OwnTracks payload type for a stored location row."""

    return str((location.raw_payload or {}).get("_type") or "").strip().casefold()


def _transition_event(location: OwnTracksLocation) -> str | None:
    """Return a normalized waypoint transition event name when the payload has one."""

    if _payload_type(location) != "transition":
        return None
    event = str((location.raw_payload or {}).get("event") or "").strip().casefold()
    if event in {"enter", "arrive", "arrival"}:
        return "enter"
    if event in {"leave", "exit", "departure"}:
        return "leave"
    return None


def _site_indexes(sites: list[Site]) -> tuple[dict[str, Site], dict[str, Site]]:
    """Build lookup tables used to match OwnTracks payload names and region ids to saved sites."""

    sites_by_name = {site.name.casefold(): site for site in sites}
    sites_by_region_id = {
        site.owntracks_region_id: site
        for site in sites
        if site.owntracks_region_id is not None
    }
    return sites_by_name, sites_by_region_id


def _site_for_location(
    location: OwnTracksLocation,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
) -> Site | None:
    """Match a non-leave OwnTracks row to a saved site using normal waypoint matching rules."""

    if _transition_event(location) == "leave":
        return None
    return site_for_location(location, sites, sites_by_name, sites_by_region_id)


def _distance_from_previous_miles(
    previous_location: OwnTracksLocation | None,
    location: OwnTracksLocation,
) -> Decimal | None:
    """Return point-to-point distance from the previous OwnTracks event."""

    if previous_location is None:
        return None
    if datetime_to_utc(location.captured_at) <= datetime_to_utc(previous_location.captured_at):
        return None
    return haversine_miles(
        previous_location.latitude,
        previous_location.longitude,
        location.latitude,
        location.longitude,
    )


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> int | None:
    """Return a non-negative elapsed second count between two timestamps."""

    if start is None or end is None:
        return None
    seconds = int((datetime_to_utc(end) - datetime_to_utc(start)).total_seconds())
    return max(seconds, 0)


def _format_elapsed_seconds(seconds: int | None) -> str:
    """Format an elapsed duration for compact Diagnostics table display."""

    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds} sec"

    minutes, _seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return f"{hours} hr {minutes} min"
        return f"{hours} hr"

    days, hours = divmod(hours, 24)
    if hours:
        return f"{days} day {hours} hr" if days == 1 else f"{days} days {hours} hr"
    return f"{days} day" if days == 1 else f"{days} days"


def _append_state_change(
    state_changes: list[OwnTracksStateChange],
    location: OwnTracksLocation,
    *,
    state: str,
    label: str,
    source: str,
    site_name: str | None = None,
    distance_miles: Decimal | None = None,
) -> None:
    """Append one Diagnostics state change with shared event metadata."""

    previous_change = state_changes[-1] if state_changes else None
    state_changes.append(
        OwnTracksStateChange(
            captured_at=location.captured_at,
            state=state,
            label=label,
            site_name=site_name,
            duration_seconds=_elapsed_seconds(
                previous_change.captured_at if previous_change is not None else None,
                location.captured_at,
            ),
            source=source,
            received_delay_seconds=_elapsed_seconds(location.captured_at, location.received_at),
            odometer_miles=location.odometer_miles,
            distance_miles=distance_miles,
        )
    )


def _latest_arrival_at_for_site(
    db: Session,
    site: Site,
    sites: list[Site],
    sites_by_name: dict[str, Site],
    sites_by_region_id: dict[str, Site],
    *,
    before_or_at: datetime | None,
    transition_limit: int = 200,
) -> datetime | None:
    """Find the latest saved OwnTracks enter transition for the displayed current site."""

    # Transition rows are sparse compared with location rows, so this query stays bounded while
    # still reaching farther back than the high-volume movement history used for state replay.
    query = (
        select(OwnTracksLocation)
        .where(OwnTracksLocation.raw_payload["_type"].as_string() == "transition")
        .order_by(OwnTracksLocation.captured_at.desc(), OwnTracksLocation.id.desc())
        .limit(transition_limit)
    )
    if before_or_at is not None:
        query = query.where(OwnTracksLocation.captured_at <= before_or_at)

    for location in db.scalars(query):
        if _transition_event(location) != "enter":
            continue
        event_site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        if event_site is not None and event_site.id == site.id:
            return location.captured_at
    return None


def owntracks_movement_diagnostics(
    db: Session,
    *,
    history_limit: int = 500,
    state_change_limit: int = 30,
) -> OwnTracksMovementDiagnostics:
    """Infer current waypoint/travel state and recent state changes from OwnTracks rows."""

    settings = get_settings()
    # The state machine needs chronological rows, but loading newest first keeps the query bounded.
    newest_locations = list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.id.desc())
            .limit(history_limit)
        )
    )
    locations = list(reversed(newest_locations))
    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = _site_indexes(sites)

    # State variables represent the latest known position while replaying stored OwnTracks rows.
    state_changes: list[OwnTracksStateChange] = []
    current_site: Site | None = None
    arrived_at: datetime | None = None
    travel_active = False
    latest_distance_miles: Decimal | None = None
    latest_detected_at: datetime | None = None
    previous_location: OwnTracksLocation | None = None

    for location in locations:
        event = _transition_event(location)
        event_site = site_for_location(location, sites, sites_by_name, sites_by_region_id)
        location_site = _site_for_location(location, sites, sites_by_name, sites_by_region_id)
        distance_miles = _distance_from_previous_miles(previous_location, location)
        latest_distance_miles = distance_miles
        latest_detected_at = location.captured_at

        if event == "enter" and event_site is not None:
            current_site = event_site
            arrived_at = location.captured_at
            travel_active = False
            _append_state_change(
                state_changes,
                location,
                state="arrived",
                label="Arrived at waypoint",
                site_name=event_site.name,
                source="OwnTracks transition",
            )
        elif event == "leave" and event_site is not None:
            if current_site is not None and current_site.id == event_site.id:
                current_site = None
                arrived_at = None
            travel_active = False
            _append_state_change(
                state_changes,
                location,
                state="left",
                label="Left waypoint",
                site_name=event_site.name,
                source="OwnTracks transition",
            )
        elif location_site is not None:
            if current_site is None or current_site.id != location_site.id:
                current_site = location_site
                arrived_at = location.captured_at
                if previous_location is not None:
                    _append_state_change(
                        state_changes,
                        location,
                        state="arrived",
                        label="Arrived at waypoint",
                        site_name=location_site.name,
                        source="Location inference",
                    )
            travel_active = False
        else:
            if current_site is not None:
                _append_state_change(
                    state_changes,
                    location,
                    state="left",
                    label="Left waypoint",
                    site_name=current_site.name,
                    source="Location inference",
                )
                current_site = None
                arrived_at = None

            traveled_meters = (
                distance_miles * METERS_PER_MILE if distance_miles is not None else None
            )
            if (
                traveled_meters is not None
                and traveled_meters >= settings.owntracks_travel_distance_m
                and not travel_active
            ):
                travel_active = True
                _append_state_change(
                    state_changes,
                    location,
                    state="travel",
                    label="Travel detected",
                    source="Movement threshold",
                    distance_miles=distance_miles,
                )

        previous_location = location

    if current_site is not None:
        resolved_arrived_at = _latest_arrival_at_for_site(
            db,
            current_site,
            sites,
            sites_by_name,
            sites_by_region_id,
            before_or_at=latest_detected_at,
        )
        current_state = CurrentOwnTracksState(
            state="waypoint",
            label="Inside waypoint",
            site_name=current_site.name,
            arrived_at=resolved_arrived_at or arrived_at or current_site.last_visited_at,
            detected_at=latest_detected_at,
            distance_miles=latest_distance_miles,
        )
    elif travel_active:
        current_state = CurrentOwnTracksState(
            state="travel",
            label="Travel detected",
            detected_at=latest_detected_at,
            distance_miles=latest_distance_miles,
        )
    elif locations:
        current_state = CurrentOwnTracksState(
            state="away",
            label="Away from saved waypoints",
            detected_at=latest_detected_at,
            distance_miles=latest_distance_miles,
        )
    else:
        current_state = CurrentOwnTracksState(state="unknown", label="No OwnTracks data")

    return OwnTracksMovementDiagnostics(
        current_state=current_state,
        state_changes=list(reversed(state_changes[-state_change_limit:])),
    )
