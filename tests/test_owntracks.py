import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from trip_tracker.models import Base, OwnTracksLocation, Site, Trip
from trip_tracker.services.owntracks import parse_owntracks_location, process_owntracks_payload


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_parse_owntracks_location_payload() -> None:
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": 1_718_000_000,
        "tid": "IP",
        "acc": 12,
        "batt": 88,
        "topic": "owntracks/me/android",
    }

    message = parse_owntracks_location(json.dumps(payload).encode("utf-8"))

    assert message.identity.user == "me"
    assert message.identity.device == "android"
    assert message.tracker_id == "IP"
    assert message.accuracy_m == 12
    assert str(message.latitude) == "42.3314"


def test_process_owntracks_waypoint_creates_site() -> None:
    db = _session()
    payload = {
        "_type": "waypoint",
        "desc": "Client Warehouse",
        "lat": 42.3314,
        "lon": -83.0458,
        "rad": "75",
        "tst": 1_718_000_000,
        "rid": "abc123",
    }

    result = process_owntracks_payload(db, json.dumps(payload).encode("utf-8"))
    site = db.scalar(select(Site).where(Site.name == "Client Warehouse"))

    assert result.location is None
    assert site is not None
    assert site.radius_m == 75
    assert site.latitude == Decimal("42.3314")
    assert site.owntracks_region_id == "abc123"


def test_process_owntracks_location_with_region_does_not_create_waypoint() -> None:
    db = _session()
    current_time = datetime.now(UTC)
    before_receive = datetime.now(UTC)
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": int(current_time.timestamp()),
        "inregions": ["Client Office"],
    }

    result = process_owntracks_payload(db, json.dumps(payload).encode("utf-8"))
    after_receive = datetime.now(UTC)
    site = db.scalar(select(Site).where(Site.name == "Client Office"))
    received_at = result.location.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)

    assert result.location is not None
    assert site is None
    assert db.scalar(select(OwnTracksLocation.id)) is not None
    assert result.location.captured_at.replace(tzinfo=UTC) == current_time.replace(microsecond=0)
    assert before_receive <= received_at <= after_receive


def test_process_owntracks_payload_automatically_creates_trip() -> None:
    db = _session()
    day = datetime(2030, 1, 1, 13, 0, tzinfo=UTC)
    db.add_all(
        [
            Site(
                name="Client A",
                latitude=Decimal("42.3314"),
                longitude=Decimal("-83.0458"),
                radius_m=120,
            ),
            Site(
                name="Client B",
                latitude=Decimal("42.3440"),
                longitude=Decimal("-83.0600"),
                radius_m=120,
            ),
        ]
    )
    db.commit()

    for captured_at, latitude, longitude, event, desc in [
        (day, 42.3314, -83.0458, "leave", "Client A"),
        (day + timedelta(minutes=25), 42.3440, -83.0600, "enter", "Client B"),
    ]:
        process_owntracks_payload(
            db,
            json.dumps(
                {
                    "_type": "transition",
                    "event": event,
                    "desc": desc,
                    "lat": latitude,
                    "lon": longitude,
                    "tst": int(captured_at.timestamp()),
                }
            ).encode("utf-8"),
        )
    process_owntracks_payload(
        db,
        json.dumps(
            {
                "_type": "location",
                "lat": 42.3440,
                "lon": -83.0600,
                "inregions": ["Client B"],
                "tst": int((day + timedelta(minutes=30)).timestamp()),
            }
        ).encode("utf-8"),
    )

    trips = list(db.scalars(select(Trip).order_by(Trip.started_at.asc())))
    client_a = db.scalar(select(Site).where(Site.name == "Client A"))
    client_b = db.scalar(select(Site).where(Site.name == "Client B"))
    assert len(trips) == 1
    assert trips[0].trip_date == day.date()
    assert trips[0].miles > Decimal("0.00")
    assert client_a is not None
    assert client_a.last_visited_at is None
    assert client_b is not None
    assert client_b.last_visited_at.replace(tzinfo=UTC) == day + timedelta(minutes=25)
