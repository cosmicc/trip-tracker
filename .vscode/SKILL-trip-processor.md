# Skill: Understanding Trip Processor and Automatic Trip Generation

**Purpose**: Guide AI agents through the automatic trip generation system, trip processor behavior, debugging, and extending trip detection logic.

## Overview

The trip processor (`trip_tracker/services/trip_processor.py`) is the core engine that:
1. Monitors incoming OwnTracks location and transition events
2. Detects qualifying waypoint transition pairs (leave + enter)
3. Generates `Trip` records with calculated mileage
4. Maintains a rolling odometer checkpoint
5. Purges only old raw OwnTracks location/event records after the 90-day minimum retention window

OwnTracks ingestion is HTTP-only. The endpoint commits raw messages directly to PostgreSQL and
returns `200 []` only after acceptance. When PostgreSQL or migrations are unavailable, it returns a
retryable `503` so the OwnTracks mobile app retains and resends its own queue. Exact HTTP retries
must not create duplicate raw rows. There is no server-side buffer, replay worker, or MQTT path.

---

## Trip Generation Logic

### How Trips Are Created

A trip is created when:
```
waypoint_leave_event (at time T1) 
  + waypoint_enter_event (at time T2) 
  + T2 > T1 
  + destination remains valid for ≥ OWNTRACKS_WAYPOINT_DWELL_MINUTES
  + NOT (origin == "Home" AND destination == "Home")
```

A waypoint `leave` is a valid trip origin only when it is the first usable leave in the processed
range or follows a dwell-confirmed arrival for that same waypoint. If a waypoint `enter` is rejected
because the device leaves before the dwell deadline, the follow-up `leave` must not be reused as
the origin for a return trip.

OwnTracks region metadata is only a candidate signal. A destination visit that starts from stored
latitude/longitude inside the saved waypoint radius can be confirmed by later coordinates inside
the radius, a later same-waypoint `leave`, a later next-waypoint `enter`, or the next processing
pass after the dwell timer when no earlier event contradicts the visit. An OwnTracks-named arrival
whose first coordinates are outside the saved radius can still be confirmed by later same-waypoint
state evidence, such as a same-waypoint `leave` after the dwell window. `desc`, `rid`, or
`inregions` labels alone must not override outside-radius coordinates without that later state
confirmation.

A trip that begins before local midnight and arrives after midnight belongs to its local start day.
Load enough lookahead to include the destination arrival, dwell confirmation, and OwnTracks path
points through the arrival without starting unrelated next-day trips during the prior-day pass.

### Key Entry Point

[`generate_trips(db, day, checkpoint)`](trip_tracker/services/mileage.py) in `mileage.py`:
- Called once per local calendar day by the trip processor
- Returns list of newly created `Trip` records
- Uses a `TripProcessingCheckpoint` to avoid re-processing old data

### Event Sequence Example

```
1. OwnTracks sends: transition event { "event": "leave", "desc": "Home", ... }
   → Stored in owntracks_locations as raw_payload

2. OwnTracks sends: 20 location updates inside "Work" waypoint
   → Each stored in owntracks_locations

3. OwnTracks sends: transition event { "event": "enter", "desc": "Work", ... }
   → Stored in owntracks_locations
   → Minimum dwell (5 min default) verified by later coordinates, later waypoint state, or the next
     processing pass after the dwell timer when no earlier event contradicts the visit
   → Trip auto-created from Home→Work

4. Trip processor updates checkpoint.last_owntracks_location_id
   → Next run skips already-processed locations
```

Before inserting an automatic trip, generation checks both database uniqueness contracts: the
exact source-event signature and the nonblank day/route/distance/odometer signature. A shifted
transition pair that matches an existing recorded-value signature must reuse the existing row;
letting PostgreSQL raise would roll back the whole processing pass and repeatedly block later days.

---

## Odometer Checkpoint System

### Rolling Odometer Calculation

The checkpoint maintains:
- `odometer_anchor_miles` — Last known absolute odometer value
- `odometer_anchor_recorded_at` — When that value was recorded
- `last_owntracks_location_id` — Position in location stream (prevents re-processing)

### Odometer Advancement

When trip processor runs:
1. Fetch new locations since last checkpoint
2. Sum point-to-point distances using Haversine formula
3. Advance rolling checkpoint: `new_checkpoint = anchor + distance_sum`
4. Stamp processed OwnTracks rows with the rolling odometer value for that point, whether or not
   the movement becomes a trip
5. Use stamped rolling odometer values for generated trip starts when available. If a generated
   trip has no stamped transition odometer yet, use the master rolling OwnTracks checkpoint before
   the trip start. If the available master checkpoint is later than the trip start, estimate the
   start only when retained OwnTracks path rows connect the trip start to that checkpoint. Prior
   trip end odometers are not a source for generated trip starts. End odometers are calculated from
   the chosen start plus the generated trip distance.
6. Use the current rolling checkpoint for new manual trip starts instead of the previous trip end
   odometer
7. Run the missing-trip-odometer backfill pass so existing rows with blank odometers can be filled
   from the master checkpoint when retained OwnTracks path data is available.
8. Generated, edited, deleted, resequenced, and backfilled trip rows never update the master rolling
   checkpoint. Only OwnTracks distance processing and an explicit manual odometer entry may update
   it. When a manual reading is entered while OwnTracks reports the vehicle inside the exact `Home`
   waypoint. Refuse normal manual saves away from Home. At Home, align all trip display odometers
   backward from that reading while preserving trip miles and every positive between-trip gap.
   The separate Emergency Rebuild action remains available away from Home, creates a full backup,
   preserves trip distances, and repairs or discards corrupt gaps before updating the master.
9. Before old raw OwnTracks rows are purged, refresh monthly OwnTracks summary rollups so older
   month web totals and event counts remain stable after raw location/event cleanup.

### Example

```
Initial state:
  odometer_anchor_miles = 50000.0
  
Location 1→2 distance: 5.2 mi
  → checkpoint becomes 50005.2
  
Location 2→3 distance: 3.1 mi
  → checkpoint becomes 50008.3

If user then enters "manual odometer: 50010.0":
  → anchor resets to 50010.0
  → next distances calculate from 50010.0
```

---

## Debugging Trip Generation

### Common Issues

**1. No trips being generated**
- Check `AUTOMATIC_TRIP_PROCESSING_ENABLED=true` in `.env`
- Verify OwnTracks is sending transition events (not just locations)
- Check minimum dwell time: the destination arrival must stay uncontradicted for at least 5
  minutes; outside-radius OwnTracks-named arrivals need later same-waypoint state evidence
- Confirm waypoint names match exactly (case-sensitive)

**2. Trips generated but with wrong mileage**
- Check mileage priority:
  1. OwnTracks path distance (preferred)
  2. Waypoint-to-waypoint distance (fallback)
- Odometer values are display/checkpoint values and must not be used as generated trip distance
- Manual edit on `/trips` page overrides calculation

**3. Trip dwell time not met**
- Default: `OWNTRACKS_WAYPOINT_DWELL_MINUTES=5`
- If user drives through a waypoint quickly, trip won't generate
- Check OwnTracks event timestamps: `tst` field must show no early same-waypoint leave,
  next-waypoint arrival, or clearly-away movement before the dwell deadline. A later
  same-waypoint leave after the dwell window confirms that earlier arrival, including
  OwnTracks-named arrivals whose first coordinates were outside the saved radius. An early
  same-waypoint leave rejects the arrival and cannot become the next trip origin.

### Diagnostics Page

Visit `/diagnostics` to see:
- Current OwnTracks state (at waypoint, traveling, etc.)
- Recent events (transitions and location updates)
- Recent app logs
- Recent trip calculation logs

### Trip Calculation Logger

Enable debug logging to see trip calculation details:
```env
LOG_LEVEL=debug
```

Logs go to the `trip_tracker.trip_calculation` logger through the root console handler. Use
`docker compose logs -f ttapp`
or `docker service logs -f <stack>_ttapp`; do not add a trip-calculation file handler.

---

## Code Structure

### Main Classes

**`AutomaticTripProcessor`** — Background thread that runs trip generation on interval
- `start()` — Begin background thread
- `stop()` — Stop gracefully
- Runs every `AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS` (default 60)

**`TripProcessingCheckpoint`** — Database model tracking processing state
- `name` — Always `"automatic_trip_processing"`
- `last_owntracks_location_id` — Prevents re-processing
- `odometer_anchor_miles` — Rolling odometer value
- `odometer_anchor_recorded_at` — When anchor was recorded

**`TripGenerationKey`** — Tuple identifying a unique trip: `(origin_id, dest_id, started_at, ended_at)`

### Key Functions

**`_new_locations_after_checkpoint(db, checkpoint)`**
- Returns list of unprocessed OwnTracks location rows since last checkpoint
- Used to detect new events

**`update_odometer_anchor_from_manual_reading(db, odometer_miles, recorded_at)`**
- Called when user manually enters odometer on `/diagnostics` page
- Resets rolling checkpoint to exact value

---

## Extending Trip Generation

### Adding Custom Trip Detection Logic

If you need to generate trips from sources other than OwnTracks:

1. **For manual trips**: Use [`create_manual_trip()`](trip_tracker/services/mileage.py#L400) in mileage.py
   ```python
   trip = create_manual_trip(
       db,
       trip_date,
       origin_name,
       destination_name,
       start_lat, start_lon,
       end_lat, end_lon,
       miles
   )
   ```

2. **To skip a trip**: Use [`delete_trip()`](trip_tracker/services/mileage.py#L450) to create a deletion tombstone
   - Prevents auto-regeneration from same OwnTracks events

3. **To edit a trip**: Use [`update_trip_details()`](trip_tracker/services/mileage.py#L480)
   - Updates trip date, names, or miles
   - Re-sequences month's odometer chain when miles change

### Modifying Waypoint Matching

Edit `site_for_location()` in [mileage.py](trip_tracker/services/mileage.py#L250) to customize how OwnTracks events match to saved waypoints:
- Currently matches by: region ID → name → distance (within radius)
- Can add custom rules (e.g., time-of-day, frequency bias)

### Adding Custom Odometer Source

The mileage calculation supports custom odometer sources. Add a new source type:
1. Edit mileage calculation in `_calculate_mileage_for_trip()`
2. Set `start_odometer_source` and `end_odometer_source` accordingly
3. Odometer display will use the source name in web UI

---

## Configuration

Key settings for trip processing:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTOMATIC_TRIP_PROCESSING_ENABLED` | `true` | Enable/disable background processor |
| `AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS` | `60` | How often to run trip generation |
| `OWNTRACKS_WAYPOINT_DWELL_MINUTES` | `5` | Minimum time inside destination before trip confirmed |
| `OWNTRACKS_LOCATION_RETENTION_DAYS` | `90` | Days to keep raw OwnTracks location/event records before purging; values below 90 are treated as 90 |
| `OWNTRACKS_PURGE_ENABLED` | `true` | Enable/disable automatic purge |
| `LOCAL_TIMEZONE` | `America/Detroit` | Timezone for trip date selection |

---

## Testing

See [test_mileage.py](tests/test_mileage.py) for comprehensive trip generation tests:
- Trip detection from waypoint transitions
- Odometer calculation and advancement
- Manual trip entry
- Trip deletion and suppression records
- Mileage fallback priority system

Key test patterns:
```python
# Create mock locations and transitions
# Call generate_trips(db, day, checkpoint)
# Assert Trip records created with correct mileage
# Verify checkpoint advanced correctly
```

---

## Performance Considerations

- **Database queries**: Trip processor runs once per minute (configurable), one query per unprocessed location
- **OwnTracks retention**: Purge keeps at least the last 90 days of raw OwnTracks rows and stores
  monthly summaries before cleanup so older month web totals remain stable
- **Checkpoint**: One row in database, updated once per processor run
- **Lock**: Single thread processing to prevent concurrent modification conflicts

See [trip_processor.py](trip_tracker/services/trip_processor.py#L1) `_PROCESSING_LOCK` for concurrency guard.
