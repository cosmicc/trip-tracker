import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from trip_tracker.models import Site


def _epoch_seconds(value: datetime | None) -> int:
    if value is None:
        return int(datetime.now(UTC).timestamp())
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp())


def _coordinate(value: Decimal) -> float:
    return float(value)


def owntracks_waypoint(site: Site) -> dict[str, object]:
    timestamp = _epoch_seconds(site.created_at)
    return {
        "_type": "waypoint",
        "tst": timestamp,
        "rid": site.owntracks_region_id or f"ml-{site.id}",
        "desc": site.name,
        "rad": site.radius_m,
        "lat": _coordinate(site.latitude),
        "lon": _coordinate(site.longitude),
        "wtst": timestamp,
    }


def owntracks_waypoints_export(sites: Iterable[Site]) -> dict[str, object]:
    return {
        "_type": "waypoints",
        "waypoints": [owntracks_waypoint(site) for site in sites],
    }


def owntracks_waypoints_json(sites: Iterable[Site]) -> str:
    return json.dumps(owntracks_waypoints_export(sites), indent=2) + "\n"
