from datetime import UTC, datetime
from decimal import Decimal

from trip_tracker.models import Site
from trip_tracker.services.waypoints import owntracks_waypoints_export


def test_owntracks_waypoints_export_uses_importable_waypoint_shape() -> None:
    waypoint = Site(
        id=7,
        name="Client Warehouse",
        owntracks_region_id="abc123",
        latitude=Decimal("42.3314000"),
        longitude=Decimal("-83.0458000"),
        radius_m=75,
        created_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
    )

    payload = owntracks_waypoints_export([waypoint])

    assert payload == {
        "_type": "waypoints",
        "waypoints": [
            {
                "_type": "waypoint",
                "tst": 1_781_179_200,
                "rid": "abc123",
                "desc": "Client Warehouse",
                "rad": 75,
                "lat": 42.3314,
                "lon": -83.0458,
                "wtst": 1_781_179_200,
            }
        ],
    }
