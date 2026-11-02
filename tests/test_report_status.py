"""Report workflow, immutable submitted months, and odometer continuity regressions."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trip_tracker.models import Base, MonthlyReportExpense, MonthlyReportStatus, Site, Trip
from trip_tracker.services.backups import create_full_backup, restore_full_backup
from trip_tracker.services.mileage import (
    delete_trip,
    repair_overlapping_trip_odometers,
    resequence_month_trip_odometers_from,
)
from trip_tracker.services.report_status import (
    month_is_submitted,
    record_report_download,
    set_report_submitted,
)


@pytest.fixture
def db(monkeypatch):
    """Provide isolated schema and a fixed local current month."""
    monkeypatch.setattr(
        "trip_tracker.services.report_status.local_today", lambda: date(2026, 11, 1)
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_trip(db, day, start, miles="10.0"):
    """Save a trip with explicit display odometers and unchanged recorded mileage."""
    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=16)
    trip = Trip(
        trip_date=day,
        started_at=started,
        ended_at=started + timedelta(minutes=30),
        start_latitude=0,
        start_longitude=0,
        end_latitude=1,
        end_longitude=1,
        miles=Decimal(miles),
        start_odometer_miles=Decimal(start),
        end_odometer_miles=Decimal(start) + Decimal(miles),
        source="manual",
    )
    db.add(trip)
    db.commit()
    return trip


def test_repairs_three_equal_starts_preserving_miles_and_valid_gaps(db):
    trips = [make_trip(db, date(2026, 10, day), "1000.0") for day in (28, 29, 30)]
    later = make_trip(db, date(2026, 11, 1), "1100.0", "7.0")
    assert repair_overlapping_trip_odometers(db) == 2
    db.commit()
    assert [trip.start_odometer_miles for trip in trips] == list(map(Decimal, (1000, 1010, 1020)))
    assert [trip.end_odometer_miles for trip in trips] == list(map(Decimal, (1010, 1020, 1030)))
    assert [trip.miles for trip in trips] == [Decimal("10.0")] * 3
    assert later.start_odometer_miles == Decimal("1100.0")
    assert repair_overlapping_trip_odometers(db) == 0


def test_edit_clamps_to_previous_end_and_propagates_across_month_with_gaps(db):
    prior = make_trip(db, date(2026, 10, 29), "1000.0")
    edited = make_trip(db, date(2026, 10, 30), "1000.0")
    later = make_trip(db, date(2026, 11, 1), "1015.0")
    edited.miles = Decimal("20.0")
    resequence_month_trip_odometers_from(db, edited)
    db.commit()
    assert prior.end_odometer_miles == Decimal("1010.0")
    assert edited.start_odometer_miles == Decimal("1010.0")
    assert edited.end_odometer_miles == Decimal("1030.0")
    assert later.start_odometer_miles == Decimal("1035.0")
    assert later.end_odometer_miles == Decimal("1045.0")
    assert later.miles == Decimal("10.0")


def test_deletion_does_not_adjust_other_odometers(db):
    first = make_trip(db, date(2026, 10, 29), "1000.0")
    later = make_trip(db, date(2026, 10, 30), "1010.0")
    delete_trip(db, first)
    db.commit()
    assert (later.start_odometer_miles, later.end_odometer_miles) == (Decimal(1010), Decimal(1020))
    assert repair_overlapping_trip_odometers(db) == 0


def test_submission_requires_finished_month_and_current_pdf(db):
    trip = make_trip(db, date(2026, 10, 29), "1000.0")
    with pytest.raises(HTTPException, match="fresh PDF"):
        set_report_submitted(db, 2026, 10, True)
    record_report_download(db, 2026, 10)
    trip.miles = Decimal("20.0")
    db.commit()
    with pytest.raises(HTTPException, match="fresh PDF"):
        set_report_submitted(db, 2026, 10, True)
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    assert month_is_submitted(db, 2026, 10)
    record_report_download(db, 2026, 11)
    with pytest.raises(HTTPException, match="must finish"):
        set_report_submitted(db, 2026, 11, True)


@pytest.mark.parametrize("operation", ["edit", "delete", "expense", "insert"])
def test_submitted_month_rejects_writes(db, operation):
    trip = make_trip(db, date(2026, 10, 29), "1000.0")
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    if operation == "edit":
        trip.miles = Decimal("99.0")
    elif operation == "delete":
        db.delete(trip)
    elif operation == "expense":
        db.add(
            MonthlyReportExpense(
                expense_date=date(2026, 10, 29),
                year=2026,
                month=10,
                reason="Parking",
                amount=Decimal("5.00"),
            )
        )
    else:
        trip = Trip(trip_date=date(2026, 10, 29))
        db.add(trip)
    with pytest.raises(HTTPException, match="submitted"):
        db.commit()
    db.rollback()


def test_cross_month_edit_is_rolled_back_when_later_month_is_submitted(db):
    earlier = make_trip(db, date(2026, 9, 30), "1000.0")
    later = make_trip(db, date(2026, 10, 1), "1010.0")
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    earlier.miles = Decimal("20.0")
    with pytest.raises(HTTPException, match="2026-10"):
        resequence_month_trip_odometers_from(db, earlier)
        db.commit()
    db.rollback()
    assert earlier.miles == Decimal("10.0")
    assert later.start_odometer_miles == Decimal("1010.0")
    set_report_submitted(db, 2026, 10, False)
    earlier.miles = Decimal("20.0")
    resequence_month_trip_odometers_from(db, earlier)
    db.commit()
    assert later.start_odometer_miles == Decimal("1020.0")
    with pytest.raises(HTTPException, match="fresh PDF"):
        set_report_submitted(db, 2026, 10, True)


def test_backup_status_roundtrip_and_pre153_backup_compatibility(db):
    make_trip(db, date(2026, 10, 29), "1000.0")
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    backup = create_full_backup(db)
    restore_full_backup(db, backup.content)
    db.expire_all()
    assert month_is_submitted(db, 2026, 10)
    payload = json.loads(gzip.decompress(backup.content))
    del payload["tables"]["monthly_report_statuses"]
    restore_full_backup(db, gzip.compress(json.dumps(payload).encode()))
    db.expire_all()
    assert db.scalar(select(MonthlyReportStatus)) is None
    assert len(list(db.scalars(select(Trip)))) == 1


def test_overlap_repair_is_blocked_at_submitted_boundary(db):
    """Maintenance cannot alter the chronology leading into a submitted month."""
    prior = make_trip(db, date(2026, 9, 30), "1000.0")
    later = make_trip(db, date(2026, 10, 1), "1010.0")
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    prior.miles = Decimal("20.0")
    with pytest.raises(HTTPException, match="2026-10"):
        repair_overlapping_trip_odometers(db)
    db.rollback()
    assert later.start_odometer_miles == Decimal("1010.0")
    assert prior.miles == Decimal("10.0")


def test_mileage_decrease_preserves_positive_gap(db):
    """A shorter edited trip shifts later readings while retaining non-trip mileage."""
    edited = make_trip(db, date(2026, 9, 30), "1000.0", "20.0")
    later = make_trip(db, date(2026, 10, 1), "1025.0")
    edited.miles = Decimal("10.0")
    resequence_month_trip_odometers_from(db, edited)
    db.commit()
    assert later.start_odometer_miles == Decimal("1015.0")
    assert later.end_odometer_miles == Decimal("1025.0")
    assert later.miles == Decimal("10.0")


def test_legacy_waypoint_names_invalidate_downloads_and_obey_submission_lock(db):
    """Protect legacy reports that display names from their related waypoint."""
    site = Site(name="Office", latitude=42, longitude=-83, radius_m=150)
    db.add(site)
    db.commit()
    trip = make_trip(db, date(2026, 10, 29), "1000.0")
    trip.destination_site_id = site.id
    trip.destination_name = None
    db.commit()
    record_report_download(db, 2026, 10)
    site.name = "New Office"
    db.commit()
    with pytest.raises(HTTPException, match="fresh PDF"):
        set_report_submitted(db, 2026, 10, True)
    record_report_download(db, 2026, 10)
    set_report_submitted(db, 2026, 10, True)
    site.name = "Another Office"
    with pytest.raises(HTTPException, match="submitted"):
        db.commit()
    db.rollback()
    assert site.name == "New Office"
