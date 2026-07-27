# Skill: Database Migrations and Schema Changes

**Purpose**: Guide AI agents through adding database fields, creating migrations, and maintaining the PostgreSQL schema safely.

## Overview

Trip Tracker uses [Alembic](https://alembic.sqlalchemy.org/) for database migrations:
- Migrations live in [`alembic/versions/`](alembic/versions/)
- SQLAlchemy models in [trip_tracker/models.py](trip_tracker/models.py)
- Migrations run automatically on app startup via Docker
- Manual migration commands should run through Docker Compose

The 1.5.0 product rename is an operational database-identity change, not an Alembic schema
migration. Follow [UPGRADE-1.5.md](UPGRADE-1.5.md) to rename the database and role and preserve the
existing Docker volume. Do not encode database or role renames in an Alembic revision.

---

## Adding a New Database Field

### Step 1: Update the SQLAlchemy Model

Edit [trip_tracker/models.py](trip_tracker/models.py):

```python
class Trip(Base):
    __tablename__ = "trips"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ... existing fields ...
    
    # ADD NEW FIELD HERE
    custom_field: Mapped[str] = mapped_column(String(255), nullable=True)
```

**Type mapping guide**:
- String: `Mapped[str]` → `String(length)`
- Integer: `Mapped[int]` → `Integer`
- Decimal: `Mapped[Decimal]` → `Numeric(precision, scale)`
- Date: `Mapped[date]` → `Date`
- DateTime: `Mapped[datetime]` → `DateTime(timezone=True)`
- Boolean: `Mapped[bool]` → `Boolean`
- Optional: `Mapped[str | None]` → add `nullable=True`

### Step 2: Create a Migration

In the project root:
```bash
docker compose run --rm ttapp alembic revision -m "add custom_field to trips"
```

This creates a new file in `alembic/versions/` with timestamp (e.g., `20260615_0015_add_custom_field_to_trips.py`).

### Step 3: Write the Migration Script

Open the generated file and edit the `upgrade()` and `downgrade()` functions:

```python
def upgrade() -> None:
    op.add_column('trips', sa.Column('custom_field', sa.String(255), nullable=True))

def downgrade() -> None:
    op.drop_column('trips', 'custom_field')
```

**Operation reference**:
- `op.add_column(table_name, column)` — Add field
- `op.drop_column(table_name, column_name)` — Remove field
- `op.alter_column()` — Modify field type/constraints
- `op.create_index()` — Add index
- `op.create_table()` — Create new table
- `op.drop_table()` — Remove table

### Step 4: Test In Docker

```bash
# Apply the migration
docker compose run --rm ttapp alembic upgrade head

# Verify the field exists when using the bundled local PostgreSQL profile
docker compose exec postgres psql -U triptracker -d trip_tracker -c "\d trips"
```

### Step 5: Rollback if Needed

```bash
# Revert to previous migration
docker compose run --rm ttapp alembic downgrade -1

# List migration history
docker compose run --rm ttapp alembic current
docker compose run --rm ttapp alembic history
```

---

## Migration Workflow

### Best Practices

1. **One concept per migration**
   - Don't combine unrelated changes in one migration
   - Makes rollback easier and history clearer

2. **Make fields nullable initially**
   ```python
   custom_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
   ```
   - Allows migration on existing data without data loss
   - Later: Add default or backfill before removing nullable

3. **Index foreign keys and frequently queried fields**
   ```python
   op.create_index('idx_trips_trip_date', 'trips', ['trip_date'])
   ```

4. **Test both directions through Docker**
   - `docker compose run --rm ttapp alembic upgrade head` — Apply forward
   - `docker compose run --rm ttapp alembic downgrade -1` — Verify rollback works

5. **Avoid data transformations in migration**
   - Migrations should handle schema only
   - Write a separate script for data cleanup/backfill if needed

### Common Migration Patterns

**Adding a required field with default value**:
```python
def upgrade() -> None:
    op.add_column('trips', sa.Column('status', sa.String(20), nullable=False, server_default='active'))
    op.alter_column('trips', 'status', server_default=None)

def downgrade() -> None:
    op.drop_column('trips', 'status')
```

**Renaming a column**:
```python
def upgrade() -> None:
    op.alter_column('trips', 'old_name', new_column_name='new_name')

def downgrade() -> None:
    op.alter_column('trips', 'new_name', new_column_name='old_name')
```

**Creating a unique constraint**:
```python
def upgrade() -> None:
    op.create_unique_constraint('uq_trips_external_id', 'trips', ['external_id'])

def downgrade() -> None:
    op.drop_constraint('uq_trips_external_id', 'trips')
```

**Adding a foreign key**:
```python
def upgrade() -> None:
    op.create_foreign_key('fk_trips_vehicle_id', 'trips', 'vehicles', ['vehicle_id'], ['id'])

def downgrade() -> None:
    op.drop_constraint('fk_trips_vehicle_id', 'trips')
```

---

## Existing Models Reference

### Core Models

**Trip** (`trips` table)
- Primary trip record with mileage, locations, dates
- Links to origin/destination `Site` via foreign keys
- Stores odometer values and mileage source

**Site** (`sites` table)
- Saved waypoint/location with lat/lon
- Stores OwnTracks sync metadata (`owntracks_region_id`)
- Tracks `last_visited_at` for sorting/display

**OwnTracksLocation** (`owntracks_locations` table)
- Raw event from OwnTracks (location or transition)
- Stores full JSON payload in `raw_payload`
- Indexed by `captured_at` for quick queries

**TripProcessingCheckpoint** (`trip_processing_checkpoints` table)
- Single row tracking automatic processor state
- `last_owntracks_location_id` — Prevents re-processing
- `odometer_anchor_*` — Rolling odometer value/timestamp

**GasPriceSnapshot** (`gas_price_snapshots` table)
- Daily gas price observation from provider (AAA, EIA)
- Used to calculate monthly averages

**MonthlyGasPrice** (`monthly_gas_prices` table)
- Cached monthly average for a specific month/state
- Used in PDF report calculations

**MonthlyReportExpense** (`monthly_report_expenses` table)
- Manual extra expense lines for monthly PDF reports
- Stores expense date, report year/month, reason, and amount
- Enforced in web routes at a maximum of five expenses per report month

**DeletedTrip** (`deleted_trips` table)
- Tombstone record preventing auto-recreation
- Indexes the `(origin, destination, started_at, ended_at)` tuple

**CloudflareIPBlock** (`cloudflare_ip_blocks` table)
- App-managed Cloudflare zone IP Access Rule blocks for failed-login IPs
- Stores the Cloudflare rule ID so unblock actions remove only rules this app created

**HiddenLoginFailure** (`hidden_login_failures` table)
- Diagnostics-only suppression list for failed-login audit entry IDs
- Hides rows from the UI while preserving the database audit row

**WebLoginAudit** (`web_login_audits` table)
- Stores structured successful and failed web-login audit payloads in PostgreSQL
- Uses a stable 64-character entry ID for Diagnostics hide actions and JSON Lines export

---

## Schema Inspection

### View Table Structure

Docker Compose PostgreSQL, when `COMPOSE_PROFILES=local-postgres` is enabled:
```bash
# List all tables
docker compose exec postgres psql -U triptracker -d trip_tracker -c "\dt"

# Describe a specific table
docker compose exec postgres psql -U triptracker -d trip_tracker -c "\d trips"

# View indexes
docker compose exec postgres psql -U triptracker -d trip_tracker -c "\di"
```

### From Python (SQLAlchemy)

```python
from trip_tracker.database import engine
from trip_tracker.models import Trip, Base

# Inspect table columns
table = Trip.__table__
for col in table.columns:
    print(f"{col.name}: {col.type}")
```

---

## Docker Deployment

Migrations run automatically on container startup via the app's `docker-entrypoint.sh`:

```bash
alembic upgrade head
```

The entrypoint waits for the database configured by `DATABASE_URL`, not just the bundled Compose
`postgres` container. The bundled PostgreSQL service is still the default deployment target through
`COMPOSE_PROFILES=local-postgres`, but a deployment can set `COMPOSE_PROFILES=` and point
`DATABASE_URL` at a central PostgreSQL server so Compose does not deploy the local `postgres`
service. Runtime and startup checks share `trip_tracker.database_engine.database_engine_options()`
so PostgreSQL connections use `pool_pre_ping`, configurable pool size, overflow, pool timeout,
pool recycle, connect timeout, and LIFO reuse for safer network database behavior.
Bare `postgresql://` URLs are normalized to `postgresql+psycopg://` so SQLAlchemy uses the
installed psycopg v3 driver instead of trying to import psycopg2.
Malformed `DATABASE_URL` values should be treated as database unavailable instead of crashing app
import, so database-outage mode can still start. When documenting or testing remote database
setup, remind operators to URL-encode passwords that contain reserved characters such as `@`, `:`,
`/`, or `%`.

If the database is unavailable at startup, the entrypoint starts database-outage mode instead of
exiting. OwnTracks HTTP requests receive retryable `503` responses until PostgreSQL returns.
`trip_tracker.database_migrations.run_migrations_once_on_reconnect()` runs `alembic upgrade head`
before the endpoint accepts the first recovered OwnTracks payload. Do not return `200` before this
verification and the payload commit succeed.

**To add a new migration for deployment**:
1. Create the migration through Docker:
   `docker compose run --rm ttapp alembic revision -m "description"`
2. Test with `docker compose run --rm ttapp alembic upgrade head` and
   `docker compose run --rm ttapp alembic downgrade -1`
3. Commit to git
4. On server: `docker compose up -d --build` applies migration automatically

**To manually run migration on deployed container**:
```bash
docker compose exec ttapp alembic upgrade head
docker compose exec ttapp alembic downgrade -1  # Rollback
```

---

## Common Pitfalls

1. **Forgetting `nullable=True` on new fields**
   - Migration fails because existing rows have no value for required field
   - Solution: Always use `nullable=True` initially, then remove after backfill

2. **Not testing rollback**
   - Always run `docker compose run --rm ttapp alembic downgrade -1` to verify it works
   - Prevents being stuck in an unrecoverable state

3. **Schema divergence**
   - Model and migration don't match
   - Solution: Check both files match before committing

4. **Large data migrations in alembic**
   - Very slow for big tables
   - Solution: Write separate Python script, run before/after migration

5. **Forgetting to commit migration file**
   - Migration exists locally but not in git
   - Solution: Check `git status` before deploying

---

## Testing Migrations

### In Unit Tests

Tests use SQLite in-memory database. Alembic applies migrations on test session startup:

```python
def test_with_migrations(db: Session):
    # SQLite is already at latest schema via test fixture
    trip = Trip(...)
    db.add(trip)
    db.commit()
    assert trip.custom_field is None  # New field works
```

See [tests/](tests/) for examples of database-dependent tests.

---

## References

- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLAlchemy Column Types](https://docs.sqlalchemy.org/en/20/core/types.html)
- [Existing migrations](alembic/versions/) as reference
- [models.py](trip_tracker/models.py) for field definitions
