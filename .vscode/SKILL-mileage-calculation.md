# Skill: Mileage Calculation and Odometer Management

**Purpose**: Guide AI agents through the mileage calculation system, odometer checkpoint management, and trip editing logic.

## Overview

The mileage calculation system in [trip_tracker/services/mileage.py](trip_tracker/services/mileage.py) is responsible for:
1. Calculating trip distance from OwnTracks coordinates or waypoint coordinates
2. Estimating start/end odometer values
3. Managing the rolling odometer checkpoint
4. Handling manual trip edits and suppression records

---

## Mileage Calculation Priority

When a trip is generated, mileage is determined by this priority order:

### 1. OwnTracks Path Distance (Primary)
- **Source**: Location updates between waypoint leave/enter events
- **Calculation**: Sum of point-to-point distances (Haversine formula)
- **Advantage**: Most accurate; reflects actual path taken
- **Fallback**: If fewer than 2 location updates, use next priority

### 2. Waypoint Distance (Fallback)
- **Source**: Direct distance between waypoint coordinates
- **Calculation**: Haversine distance between origin/destination sites
- **Advantage**: Always available; simple calculation
- **Limitation**: Doesn't reflect actual travel path

Odometer values are not a distance source. They are display/checkpoint values derived from
OwnTracks path distance, waypoint distance, and manual distance entries.

### Mileage Source Constants

```python
MILEAGE_SOURCE_OWNTRACKS_PATH = "owntracks_path"           # Primary
MILEAGE_SOURCE_WAYPOINT_DISTANCE = "waypoint_distance"     # Fallback
MILEAGE_SOURCE_MANUAL = "manual"                            # User override
MILEAGE_SOURCE_ESTIMATED_ODOMETER = "estimated_odometer"   # Legacy existing rows only
```

---

## Haversine Distance Calculation

The `haversine_miles()` function calculates great-circle distance:

```python
def haversine_miles(lat1, lon1, lat2, lon2) -> Decimal:
    """Calculate distance between two GPS points in miles."""
    # Uses Earth radius 6371008.8 meters
    # Returns Decimal to 0.1 mile precision
```

**Precision**: All distances quantized to 0.1 miles

**Example**:
```python
miles = haversine_miles(42.3314, -83.0458, 42.3314, -83.0417)
# Returns Decimal("5.2") (5.2 miles)
```

---

## Odometer Checkpoint System

### Data Stored

The `TripProcessingCheckpoint` maintains:

```python
class TripProcessingCheckpoint(Base):
    name = "automatic_trip_processing"              # Singleton key
    last_owntracks_location_id: int                 # Position in stream
    odometer_anchor_miles: Decimal                  # Last known odometer
    odometer_anchor_recorded_at: datetime           # When recorded (UTC)
    
    checkpoint_updated_at: datetime                 # Last checkpoint update
    last_trip_id: int | None                        # Last generated trip
```

### How Advancement Works

1. **Trip processor runs** (every 60 seconds by default)
2. **Fetch new OwnTracks locations** since `last_owntracks_location_id`
3. **For each location**, if inside a saved waypoint, ignore it (stationary noise)
4. **Sum point-to-point distances** for all other locations
5. **Advance checkpoint**: `new_odometer = anchor + distance_sum`
6. **Update `last_owntracks_location_id`** to latest processed location
7. Trip creation, deletion, resequencing, and missing-odometer backfill must not update the master
   checkpoint. Trip odometers are row display values; only OwnTracks location processing and an
   explicit manual odometer entry may move the checkpoint.

### Resetting the Anchor

When the user saves a normal manual odometer on `/diagnostics` while inside the exact `Home`
waypoint:

```python
update_odometer_anchor_from_manual_reading(
    db,
    odometer_miles=Decimal("50123.5"),
    recorded_at=datetime.now(UTC),
)
```

Effect:
- `odometer_anchor_miles` = 50123.5
- `odometer_anchor_recorded_at` = now
- Next distances calculate from 50123.5
- Normal saves are refused away from Home. At Home, all trip row odometers are aligned backward so
  the latest trip end is 50123.5. Each trip's stored mileage and every existing positive
  between-trip odometer gap remain unchanged.
- Emergency Rebuild remains available away from Home. It first creates a full backup, preserves all
  stored trip distances, uses the entered reading as the latest trip end and master checkpoint,
  reconstructs gaps at or above 200 miles from retained OwnTracks data when possible, and discards
  invalid gaps largest-first as needed to keep the sequence nonnegative.

---

## Trip Generation with Mileage

### Main Entry Point

```python
def generate_trips(
    db: Session,
    day: date,
    checkpoint: TripProcessingCheckpoint
) -> list[Trip]:
    """Generate all trips for a specific local date."""
```

### Generation Algorithm

1. **Load OwnTracks transitions** for the day (leave/enter events)
2. **Match transition pairs**:
   - `leave` at time T1 + `enter` at time T2 = potential trip
   - Verify the destination arrival remains valid for at least 5 minutes. An inside-radius arrival
     can be confirmed by later coordinates inside the radius, a later same-waypoint `leave`, a later
     next-waypoint `enter`, or the next processing pass after the dwell timer when no earlier event
     contradicts the visit. An OwnTracks-named outside-radius arrival needs later same-waypoint
     state evidence, such as a same-waypoint `leave` after the dwell window. If a same-waypoint
     `leave` happens before the dwell deadline, that visit is rejected and the `leave` must not be
     used as the origin for the next return trip.
   - Skip if both are "Home" waypoint
3. **For each valid pair**:
   - Load location updates between T1 and T2
   - Calculate mileage from OwnTracks path distance or waypoint distance
   - Use stamped rolling OwnTracks odometers for trip starts when available. If no transition
     odometer is stamped yet, use the master rolling OwnTracks checkpoint before the trip start.
     If the only available master checkpoint is later than the trip start, estimate the start from
     retained OwnTracks path rows between the trip start and that checkpoint. Do not fill from a
     later checkpoint when the needed path rows are no longer retained.
     Do not fall back to the previous trip end odometer for generated trip starts.
   - Calculate the end odometer from the chosen start odometer plus the selected trip distance
   - Create Trip record

### Mileage Calculation Function

```python
def _calculate_mileage_for_trip(
    db: Session,
    locations_between: list[OwnTracksLocation],
    origin_site: Site | None,
    destination_site: Site | None,
    checkpoint: TripProcessingCheckpoint,
) -> MileageCalculation:
    """Return miles, source, and odometer values for a trip."""
```

Returns:
```python
MileageCalculation(
    miles=Decimal("5.2"),
    mileage_source="owntracks_path",
    start_odometer_miles=Decimal("50000.0"),
    end_odometer_miles=Decimal("50005.2"),
    start_odometer_source="previous_trip",
    end_odometer_source="owntracks_path",
)
```

---

## Odometer Source Tracking

Each trip stores how the odometer values were determined:

```python
class Trip:
    start_odometer_miles: Decimal | None
    end_odometer_miles: Decimal | None
    start_odometer_source: str                 # How it was calculated
    end_odometer_source: str                   # How it was calculated
```

### Odometer Source Constants

```python
ODOMETER_SOURCE_MANUAL = "manual"                      # User entered
ODOMETER_SOURCE_ESTIMATED = "estimated"                # From checkpoint
ODOMETER_SOURCE_PREVIOUS_TRIP = "previous_trip"        # Legacy/resequenced trip row continuity
# Plus path/calculation sources...
```

### Display Format

On `/trips` page, odometer source is formatted using these labels:

```python
labels = {
    "estimated": "Estimated",
    "previous_trip": "Previous trip",
    "manual": "Manual",
    "owntracks_path": "OwnTracks path",
}
```

---

## Manual Trip Entry

### Create a Manual Trip

```python
from trip_tracker.services.mileage import create_manual_trip

trip = create_manual_trip(
    db,
    trip_date=date(2026, 6, 15),
    origin_name="Home",
    destination_name="Work",
    miles=Decimal("5.2"),
)
```

Creates a `Trip` with:
- `source="manual"`
- `mileage_source="manual"`
- Start odometer from the current rolling OwnTracks odometer checkpoint when available, not from the
  previous trip end odometer. Fall back to zero only when no master rolling checkpoint exists.
- End odometer = start odometer + entered trip miles
- New manual trips are timestamped after existing trips on the selected local date so a backdated
  manual entry lands at the end of that day, and a manual entry for today becomes the latest trip for
  today.
- Automatic resequencing of this manual trip and every later trip, even across later months, so
  future odometer fields remain cumulative after inserting a prior-date manual trip. Resequencing
  preserves existing positive odometer gaps between trips so non-trip OwnTracks distance remains
  represented instead of being collapsed into the previous trip.

The Trips web form loads saved waypoint `Site` rows and submits `origin_site_id` /
`destination_site_id` rather than free-text names. The web route passes the selected waypoint names
into `create_manual_trip()`, then stores the selected waypoint IDs and coordinates on the created
trip before committing.

---

## Editing Trip Mileage

### Update Trip Details

```python
from trip_tracker.services.mileage import update_trip_details

update_trip_details(
    trip,
    origin_name="Home",
    destination_name="Work",
    miles=Decimal("5.5"),  # Changed from 5.2
    trip_date=date(2026, 6, 15),
)
db.commit()
```

Effects:
- Updates trip fields
- Sets `mileage_source="manual"`
- Preserves the trip's creation `source`; an automatic trip with edited mileage remains an
  automatic trip and receives the purple edited-row tint on the Trips page
- Recalculates only the edited trip and later trips in the same start month. The edited trip keeps
  its existing start odometer; earlier trips and other months remain untouched.

On the Trips page, existing row dates and odometers remain read-only. From/To edits are validated
waypoint dropdown selections; changing them stores the selected waypoint IDs, names, and
coordinates on the trip without converting the trip into a manual Add Work Trip entry. The stored
miles value still drives odometer resequencing.

### Resequencing Logic

When trip miles change, recalculate from that trip forward through the same start month only:

```python
resequence_month_trip_odometers_from(db, trip)
```

When a manual trip is newly inserted, use the broader forward resequence:

```python
resequence_trip_odometers_from(db, trip)
```

This ensures:
1. The edited trip and same-month later rows are chronologically ordered
2. Manual trip start odometer comes from the current rolling checkpoint when available
3. Existing positive gaps between a previous end odometer and the next start odometer are reapplied
4. End odometer = start odometer + trip miles

Resequencing changes trip row odometer display values only. It must never move the master rolling
OwnTracks odometer checkpoint.

### Backfilling Blank Odometers

`backfill_missing_trip_odometers(db)` fills existing trip rows that are missing start or end
odometers when a master OwnTracks checkpoint and retained path rows can support the estimate. It
does not alter trip distance, route fields, source fields, deleted-trip tombstones, or the master
rolling checkpoint. Automatic trip processing runs this repair pass so recently recorded rows with
blank odometers are healed after deployment.

---

## Trip Deletion and Suppression

### Delete a Trip

```python
from trip_tracker.services.mileage import delete_trip

delete_trip(db, trip)
```

Creates a `DeletedTrip` tombstone record that prevents the same OwnTracks events from auto-recreating the trip.

### Tombstone Structure

```python
class DeletedTrip(Base):
    __table_args__ = (
        UniqueConstraint(
            "origin_site_id",
            "destination_site_id",
            "started_at",
            "ended_at",
            name="uq_deleted_trip_generation_signature",
        ),
    )
```

**Why**: Prevents duplicate trips if:
- User deletes a trip
- Later, OwnTracks retries the same HTTP events after an outage
- Trip processor re-detects the same transition pair

The live `trips` table has partial unique indexes for automatic rows on the source-event signature
`(origin_site_id, destination_site_id, started_at, ended_at)` and the nonblank recorded-value
signature `(trip_date, origin_site_id, destination_site_id, miles, start_odometer_miles,
end_odometer_miles)`. Keep the application lookup and database constraints aligned. The migration
deletes later exact automatic duplicates while retaining the oldest row. Before inserting, trip
generation must reuse an existing match from either signature so one shifted transition pair does
not raise an integrity error, roll back the checkpoint, and block later dates. The indexes do not
constrain manual trips.

---

## Waypoint Matching

### site_for_location()

Matches an OwnTracks location to a saved waypoint using this priority:

1. **OwnTracks region ID** (`rid` field)
   - Most specific; if OwnTracks sends this, use it
2. **Waypoint name match** (exact, case-insensitive)
   - From OwnTracks `desc` or `inregions` field
3. **Distance radius** (Haversine ≤ `radius_m`)
   - If location is within saved waypoint's radius, match it
4. **Closest waypoint** (if multiple match)
   - Pick the nearest one

**Example**:
```
OwnTracks location: lat=42.33, lon=-83.05
Saved sites:
  - "Home": lat=42.3314, lon=-83.0458, radius=150m → matches (5m away)
  - "Work": lat=42.33, lon=-83.00, radius=100m → no match (5.6km away)

Result: Matched to "Home"
```

---

## Constants and Precision

```python
METERS_PER_MILE = Decimal("1609.344")      # Conversion factor
EARTH_RADIUS_M = Decimal("6371008.8")      # WGS84 ellipsoid
DISTANCE_PRECISION = Decimal("0.1")        # All distances → 0.1 mile precision
ODOMETER_PRECISION = Decimal("0.1")        # All odometers → 0.1 mile precision
ROUNDING = ROUND_HALF_UP                   # Standard rounding
```

---

## Common Tasks

### Calculate Trip Mileage Manually

```python
from trip_tracker.services.mileage import haversine_miles

miles = haversine_miles(
    lat1=Decimal("42.3314"),
    lon1=Decimal("-83.0458"),
    lat2=Decimal("42.3314"),
    lon2=Decimal("-83.0417"),
)
print(f"Distance: {miles} miles")
```

### Find All Trips for a Month

```python
from trip_tracker.services.pdf import trips_for_month

trips = trips_for_month(db, year=2026, month=6)
total_miles = sum(trip.miles for trip in trips)
print(f"June 2026: {total_miles} miles across {len(trips)} trips")
```

### Get Monthly Mileage Total

```python
from trip_tracker.services.mileage import monthly_miles

total = monthly_miles(db, year=2026, month=6)
print(f"June 2026 total: {total} miles")
```

### Calculate Reimbursement

```python
from trip_tracker.services.pdf import calculate_reimbursement
from trip_tracker.config import get_settings

settings = get_settings()
reimbursement = calculate_reimbursement(
    total_miles=Decimal("500.0"),
    monthly_gas_price=Decimal("3.45"),
    vehicle_mpg=settings.vehicle_mpg,
)
print(f"Reimbursement: ${reimbursement}")
```

Dashboard reimbursement summaries should reuse this same PDF-report calculation path: monthly trip
miles from `monthly_miles()`, reimbursement gallons from `calculate_reimbursement_gallons()`, the
current saved monthly gas price, and `VEHICLE_MPG`. Display the Dashboard reimbursement gallons at
one decimal place.
`REPORT_DISPLAY_NAME` is only an optional PDF header identity value and must not affect mileage,
gas, reimbursement, dashboard totals, or report filenames.

---

## Debugging Mileage Issues

### Check Trip Mileage Source

On `/trips` page, hover over the "Trip Mi" column or check `trip.mileage_source`:
- `"owntracks_path"` — Based on location path (best)
- `"waypoint_distance"` — Direct waypoint distance (fallback)
- `"manual"` — User manually edited
- `"estimated_odometer"` — Legacy label on older rows; do not use for new distance calculations

### Inspect Odometer Chain

```python
# Get all trips for a month, in order
trips = trips_for_month(db, 2026, 6)

for i, trip in enumerate(trips, 1):
    print(f"{i}. {trip.trip_date} {trip.origin_display_name} → {trip.destination_display_name}")
    print(f"   Start: {trip.start_odometer_miles} ({trip.start_odometer_source})")
    print(f"   End: {trip.end_odometer_miles} ({trip.end_odometer_source})")
    print(f"   Miles: {trip.miles} ({trip.mileage_source})")
```

### Check Checkpoint State

```python
from trip_tracker.services.trip_processor import _get_or_create_checkpoint

checkpoint = _get_or_create_checkpoint(db)
print(f"Odometer anchor: {checkpoint.odometer_anchor_miles} miles")
print(f"Recorded at: {checkpoint.odometer_anchor_recorded_at}")
print(f"Last processed location: {checkpoint.last_owntracks_location_id}")
```

---

## Testing Mileage Calculations

See [tests/test_mileage.py](tests/test_mileage.py) for comprehensive tests:

```python
def test_trip_mileage_from_owntracks_path():
    """Trip mileage uses location path when available."""
    # Create mock locations between two waypoints
    # Call generate_trips()
    # Assert mileage_source == "owntracks_path"

def test_trip_mileage_fallback_to_waypoint_distance():
    """Falls back to waypoint distance when path unavailable."""
    # Create waypoints but no locations between
    # Call generate_trips()
    # Assert mileage_source == "waypoint_distance"
```

---

## References

- [mileage.py](trip_tracker/services/mileage.py) — Core implementation
- [trip_processor.py](trip_tracker/services/trip_processor.py) — Checkpoint management
- [models.py](trip_tracker/models.py) — Trip, Site, TripProcessingCheckpoint models
- [README.md](README.md#Trip-Detection) — Trip generation overview
