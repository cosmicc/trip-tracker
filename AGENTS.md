# AI Agent Instructions for Trip Tracker

This document helps AI coding agents understand the Trip Tracker codebase and be immediately productive.

## Project Overview

**Trip Tracker** is a FastAPI web application that:
- Receives location events from the [OwnTracks](https://owntracks.org/) mobile app
- Stores waypoint transitions in PostgreSQL
- Automatically generates **trips** from waypoint leave/enter events
- Calculates **trip mileage** using OwnTracks location path distance
- Generates monthly **PDF mileage and expense reports** with gas price calculations
- Provides a web dashboard for trip review, editing, and manual entry

**Tech Stack**: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, Jinja2, ReportLab, Docker Compose

---

## Quick Start

### Tests & Linting
```bash
pytest           # Run tests
ruff check .     # Lint
CLOUDFLARED_TUNNEL_TOKEN=dummy-token docker compose --env-file .env.docker.example config
```

### Docker Deployment / App Runtime
```bash
./scripts/init_docker_env.sh  # Generate .env with secrets
# Set CLOUDFLARED_TUNNEL_TOKEN in .env
docker compose up -d --build
```

The application is Docker-only. Do not add or document a non-Docker app runtime path.

Version 1.5.0 is the product-wide rename from Mileage Logger to Trip Tracker. Use
[UPGRADE-1.5.md](UPGRADE-1.5.md) for the approved identifier mapping and deployment migration
sequence. New code, assets, configuration, and documentation use only Trip Tracker identifiers;
pre-1.5 names are allowed only in changelog history, the migration guide, and explicit
backward-compatibility handling.

---

## Architecture

### Key Directories

| Directory | Purpose |
|-----------|---------|
| `trip_tracker/api/` | API routes for OwnTracks ingestion, trip updates, PDF export |
| `trip_tracker/web/` | Web UI routes, Jinja2 templates, HTML rendering |
| `trip_tracker/services/` | Business logic: trip generation, mileage calculation, gas prices |
| `trip_tracker/models.py` | SQLAlchemy ORM models (Trip, Site, OwnTracksLocation, etc.) |
| `alembic/versions/` | Database schema migrations |

### Core Services

**[trip_processor.py](trip_tracker/services/trip_processor.py)** — Automatic trip generation
- Watches for new OwnTracks location/transition events
- Runs `generate_trips()` when waypoint transitions occur
- Maintains rolling `TripProcessingCheckpoint` to track odometer distance
- Enforces minimum dwell time and later OwnTracks state before confirming arrival at a waypoint
- Purges only old raw OwnTracks location/event records based on `OWNTRACKS_LOCATION_RETENTION_DAYS`,
  with an enforced minimum retention of 90 days

**[mileage.py](trip_tracker/services/mileage.py)** — Trip mileage calculation
- `generate_trips()` - Core trip generation from waypoint transitions
- `haversine_miles()` - Calculates distance between GPS coordinates
- `site_for_location()` - Matches OwnTracks event to saved waypoint site
- Mileage priority: OwnTracks path distance → waypoint distance; odometer values are not a
  distance source
- Supports manual trip entry and deletion with suppression records

**[gas_prices.py](trip_tracker/services/gas_prices.py)** — Reimbursement calculation
- `GasPriceProvider` abstract class with two implementations:
  - `AaaMichiganGasPriceProvider` - Scrapes AAA website (default)
  - `EiaSeriesProvider` - Uses EIA API (requires configuration)
- Formula: `(trip_miles / VEHICLE_MPG) * gas_price = reimbursement`
- Docker runs recurring gas snapshots from the app container lifespan when
  `GAS_SNAPSHOT_ENABLED=true`; the `trip-tracker gas-snapshot` CLI remains available for manual
  or host systemd timer runs.

**[owntracks.py](trip_tracker/services/owntracks.py)** — Payload parsing
- Handles HTTP OwnTracks messages
- Parses `location` and `transition` event types
- Validates required fields: `lat`, `lon`, `tst`
- Supports OwnTracks encrypted HTTP payloads when `OWNTRACKS_ENCRYPTION_KEY` is set. The HTTP
  ingestion aliases `/api/owntracks`, `/api/owntracks/`, `/api/pub`, and `/api/pub/` then require
  both decryptable OwnTracks payloads and matching HTTP Basic Auth.
- Non-OwnTracks API routes require `Authorization: Bearer <WEB_API_KEY>` except `/api/health`,
  which stays unauthenticated for internal container health checks.
- Public web service exposes only `POST /api/owntracks`, `POST /api/owntracks/`, `POST /api/pub`,
  and `POST /api/pub/`; other API routes stay internal to the app container and Docker network,
  and still require `WEB_API_KEY` when called internally.
- Returns `503 Service Unavailable`, `Retry-After: 30`, and `Cache-Control: no-store` when
  PostgreSQL or migrations are unavailable so OwnTracks retains and retries its own HTTP queue.
- Returns `200 []` only after PostgreSQL accepts the payload. Exact HTTP retries reuse the existing
  raw event instead of inserting it twice.

**[login_failures.py](trip_tracker/services/login_failures.py)** — Web login audit logging
- Stores structured PostgreSQL records for successful and failed web UI login attempts and emits
  the same safe audit events through console logging
- Saves client IP details, submitted username, authentication method for successful logins,
  failed-login password length, user agent, request path, lockout state, and UTC/local timestamps
  without storing the raw password
- Uses the same effective client key as login lockout and Cloudflare auto-blocking. The bundled
  loopback-only web service origin passes Cloudflare's `CF-Connecting-IP` through when present; otherwise
  the app falls back to the direct client.
- Feeds the Diagnostics successful-login and failed-login tables, per-row failed-login hide
  controls, per-row Cloudflare block buttons, and the raw download endpoint; the failed-login card
  intentionally has no separate footer refresh or download buttons
- Invalid username/password browser form responses stay on `login.html` with a top status-line
  error and HTTP 200 so public browser error pages do not replace the form.
- Public-device password sessions expire after 15 minutes without browser activity, clear the
  signed session cookie and browser site data on timeout or logout, skip service-worker
  registration, and disable Device Sign-In while the login checkbox is selected. Keep the option's
  explanation in an accessible tooltip shown when the full checkbox row is hovered or focused.
- Diagnostics shows the stored effective IP for successful-login and failed-login rows. Failed-login
  row block buttons must use that same visible, blockable client IP.

**[passkeys.py](trip_tracker/services/passkeys.py)** — WebAuthn passkey login
- Generates and verifies WebAuthn registration and authentication ceremonies with `py_webauthn`
- Stores passkeys in `passkey_credentials` for the single configured `WEB_LOGIN_USERNAME`
- Keeps registration behind an authenticated Diagnostics session; unauthenticated routes are
  limited to login challenge generation and assertion verification
- Failed passkey assertions use the same audit log, temporary lockout, and Cloudflare auto-block
  path as failed password logins

**[cloudflare_blocks.py](trip_tracker/services/cloudflare_blocks.py)** — Cloudflare IP blocking
- Creates and deletes app-managed Cloudflare zone IP Access Rules for failed-login and manually
  entered IP addresses
- Uses `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, and the app-managed block table to avoid
  touching unrelated Cloudflare rules. `CLOUDFLARE_API_TOKEN` must be a Cloudflare API token with
  `Account Firewall Access Rules Write` access for the configured zone, not
  `CLOUDFLARED_TUNNEL_TOKEN` or a Global API Key.
- Enforces `CLOUDFLARE_IP_BLOCK_ALLOWLIST` so trusted IPs/CIDRs are not blocked by the app, and
  records the block reason and manual/automatic source shown on Diagnostics

**[app_health.py](trip_tracker/services/app_health.py)** — App health and Pushover alerts
- Builds the shared app-health snapshot used by Diagnostics and background notifications
- Monitors PostgreSQL availability and latency, free disk space for runtime paths, active web-login
  lockouts, and app-managed Cloudflare IP blocks
- Requires high database latency to remain elevated for
  `APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS` before Pushover alerts while Diagnostics remains live
- Sends immediate Pushover notifications when configured degraded/unavailable issue signatures
  change, repeats unchanged unhealthy states every `APP_HEALTH_REMINDER_INTERVAL_SECONDS`
  (default one hour), and sends a restored notification when all monitored checks return to healthy
- Persists notification state under `APP_HEALTH_STATE_PATH` so app restarts do not repeat the same
  degraded alert

**[pdf.py](trip_tracker/services/pdf.py)** — Report generation
- Generates portrait PDF with trip table and condensed margins for report content
- Formats the PDF title with the selected report month name and year, such as `Mileage & Expense
  Report - June 2026`
- Keeps the PDF title directly below the top margin with compact spacing between the title,
  optional submitted-by line, and trip table
- Adds optional `REPORT_DISPLAY_NAME` identification under the title as `Submitted by:` when the
  deployment setting is configured
- Highlights the total reimbursement dollar amount value cell with a soft yellow background
- Shows start/end odometers, miles, and location names
- Adds up to five manual extra expense rows after trip rows, with date, expense reason, and price,
  then includes the extra expense total in the final reimbursement total. Extra expense rows use
  the same unhighlighted background as trip rows; only the final total reimbursement value is
  highlighted.
- Escapes trip and waypoint names before passing them to ReportLab `Paragraph` so user-managed
  names, manual expense reasons, and the optional report display name render as text rather than
  PDF markup.
- Calculates total miles, mileage reimbursement, extra expense total, and total reimbursement
  amount

**[backups.py](trip_tracker/services/backups.py)** — Full app data backup and restore
- Creates a gzip-compressed JSON backup of every SQLAlchemy app table plus OwnTracks waypoint export
- Restores validated backup files transactionally by replacing current app table rows
- Creates a startup automatic backup followed by 6-hour automatic full-data backups when
  `AUTOMATIC_BACKUPS_ENABLED=true`, stores them in `AUTOMATIC_BACKUP_DIR`, and prunes to the
  newest 4 recent automatic backups plus one daily backup for each of the prior 2 days
- Pauses on shared-storage failures, including stale file handles, retries every
  `AUTOMATIC_BACKUP_RETRY_SECONDS`, and resumes the 6-hour schedule only after a successful backup
- Backs Diagnostics full backup/restore controls, retained automatic-backup downloads, and retained
  automatic-backup restore; backup download and restore require web login, restore also requires
  typed confirmation, and startup-created backup rows are labeled as Startup
- Accepts the pre-1.5 `mileage_logger.full_backup` format marker and recognizes pre-1.5 automatic
  backup filenames so a product rename never strands existing safety backups. New files must use
  Trip Tracker identifiers.

---

## Key Concepts

### Trip Generation Flow
1. OwnTracks sends waypoint transition events (enter/leave/arrival/departure)
2. Trip processor detects qualifying transitions:
   - `leave` from waypoint A + `enter` to waypoint B = one trip
   - Requires a destination arrival that remains valid for at least
     `OWNTRACKS_WAYPOINT_DWELL_MINUTES` (default 5). An inside-radius arrival can be confirmed by
     later coordinates inside the saved radius, a later same-waypoint `leave`, a later next-waypoint
     `enter`, or the next processing pass after the dwell timer when no earlier event contradicts
     the visit. An OwnTracks-named arrival whose first coordinates are outside the saved radius
     still needs later same-waypoint state evidence, such as a same-waypoint `leave` after the dwell
     window; the label alone is not enough. If an `enter` is rejected because the device leaves
     before the dwell deadline, that later `leave` must not become the origin for a return trip.
   - Home → Home never generates a trip
   - Same-waypoint trips under 1.0 mile are invalid and are suppressed with an exact deleted-trip record
3. Mileage is calculated from OwnTracks location updates between the two events
4. If OwnTracks path data is unavailable, trip distance falls back to waypoint-to-waypoint distance
5. Odometer values are display/checkpoint values: starts come from stamped rolling OwnTracks
   values when available, otherwise the master rolling OwnTracks odometer checkpoint before the
   trip start, and ends are start plus the selected trip distance. If only a later master
   checkpoint is available, missing generated-trip odometers may be estimated from retained
   OwnTracks path rows between the trip start and that checkpoint. Generated trips must not use
   prior trip end odometers as the source for a new trip start. A trip that crosses local midnight
   belongs to the local day on which it started; include OwnTracks path rows through its actual
   destination arrival after midnight.
6. Trip is stored and shown on `/trips` page for review/editing

Exact automatic generation signatures are unique at the PostgreSQL layer by origin waypoint,
destination waypoint, start time, and end time. Automatic rows also have a unique recorded-value
signature by local day, route, distance, and nonblank start/end odometers. The v1.3.4 migration
keeps the oldest existing automatic row for either duplicate signature before adding the partial
unique indexes. Application duplicate lookup must check both signatures before insertion so a
shifted duplicate transition pair cannot roll back the processing checkpoint and block later
dates. Manual trips are not restricted by those indexes.

### Odometer Checkpoint System
- Rolling odometer anchor tracks cumulative distance from OwnTracks path
- Manual odometer readings reset the anchor to an exact value
- Only OwnTracks location processing and explicit manual odometer entries update the master rolling
  odometer checkpoint. Trip creation, editing, deletion, resequencing, and odometer backfill must
  never update it; trip odometers are display state for trip rows.
- Refuse normal manual odometer saves unless the current OwnTracks state is inside the exact `Home`
  waypoint. Disable the normal Diagnostics Save button away from Home and enforce the same rule in
  the server route.
- A normal Home save aligns all trip display odometers backward from that reading so the latest
  trip end matches it. Preserve every trip's stored mileage and every existing positive odometer
  gap between trips so non-trip driving remains represented.
- Diagnostics Emergency Rebuild stays available away from Home. It must create a full backup first,
  never alter stored trip distances, use the entered reading for both the latest trip end and master
  checkpoint, reconstruct oversized gaps from retained OwnTracks rows when possible, discard gaps
  at or above 200 miles, and discard smaller gaps largest-first only when required for a valid
  nonnegative sequence.
- Manual trip starts use the current rolling OwnTracks odometer checkpoint before falling back to
  zero when no master checkpoint exists; later resequencing preserves existing positive non-trip
  odometer gaps between trips.
- Diagnostics shows the current odometer inside the Manual Odometer card before a new manual
  checkpoint value is saved
- Useful when actual odometer reading differs from GPS distance estimate
- Stored in `TripProcessingCheckpoint` table

### Timezone Handling
- All timestamps stored as UTC in database
- `LOCAL_TIMEZONE` (default `America/Detroit`) used for:
  - Trip date selection
  - Day/month boundaries
  - Dashboard display and PDF reports
- Services in `timezone.py` convert between UTC and local time

### Configuration
- **Source**: `.env` file loaded by `pydantic_settings.BaseSettings`
- **Key Variables**: `LOCAL_TIMEZONE`, `DATABASE_URL`, `DATABASE_POOL_SIZE`,
  `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT_SECONDS`, `DATABASE_POOL_RECYCLE_SECONDS`,
  `DATABASE_CONNECT_TIMEOUT_SECONDS`, `VEHICLE_MPG`,
  `REPORT_DISPLAY_NAME`,
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES`, `LOG_LEVEL`, `APP_DATA_DIR`,
  `AUTOMATIC_BACKUPS_ENABLED`, `AUTOMATIC_BACKUP_DIR`, `AUTOMATIC_BACKUP_RETRY_SECONDS`,
  `MAX_BACKUP_RESTORE_BYTES`, `GAS_SNAPSHOT_ENABLED`, `GAS_SNAPSHOT_INTERVAL_SECONDS`,
  `GAS_SNAPSHOT_RUN_ON_STARTUP`, `CLOUDFLARE_IP_BLOCKING_ENABLED`, `CLOUDFLARE_API_TOKEN`,
  `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_IP_BLOCK_ALLOWLIST`,
  `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS`, `PUSHOVER_ENABLED`, `PUSHOVER_TOKEN`,
  `PUSHOVER_USER`, `PUSHOVER_APP_KEY`, `PUSHOVER_USER_KEY`, `PUSHOVER_DEVICE`,
  `PUSHOVER_PRIORITY`, `APP_HEALTH_MONITOR_INTERVAL_SECONDS`,
  `APP_HEALTH_REMINDER_INTERVAL_SECONDS`,
  `APP_HEALTH_DB_LATENCY_WARNING_MS`, `APP_HEALTH_DB_LATENCY_CRITICAL_MS`,
  `APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS`, `APP_HEALTH_DISK_WARNING_FREE_MB`,
  `APP_HEALTH_DISK_CRITICAL_FREE_MB`,
  `APP_HEALTH_STATE_PATH`, `POSTGRES_DATA_VOLUME`, `WEB_API_KEY`,
  `OWNTRACKS_ENCRYPTION_KEY`, `PASSKEY_RP_NAME`,
  `PASSKEY_RP_ID`, `PASSKEY_ORIGIN`
- See [README.md](README.md#Useful-Docker-environment-options) for all options

### Visual Design and Color Palette
- The active app palette is defined with CSS variables in
  [styles.css](trip_tracker/web/static/styles.css).
- Saved palette samples live in [docs/design/color-palettes.svg](docs/design/color-palettes.svg).
  Option A is the current app palette; the other options are proposals only. The approved Work
  Trips row-state colors are blue (`#4BA3FF`) for automatic trips, purple (`#A855F7`) for edited
  trips, and gold (`#E2AD45`) for manual trips.
- The source app logo is saved as [docs/design/trip-tracker-logo-original.png](docs/design/trip-tracker-logo-original.png),
  with a matching SVG wrapper at [docs/design/trip-tracker-logo.svg](docs/design/trip-tracker-logo.svg).
  Additional source variants are saved as
  [docs/design/trip-tracker-logo-transparent.png](docs/design/trip-tracker-logo-transparent.png)
  and [docs/design/trip-tracker-logo-fully-transparent.png](docs/design/trip-tracker-logo-fully-transparent.png).
  Web favicon icons are generated from the original square logo. Apple touch icons and installable
  web-app icons use the cleaned transparent brand asset centered on the dark app background with
  launcher-safe padding so mobile masks do not crop the logo. The authenticated header brand uses
  the cleaned transparent brand asset under [trip_tracker/web/static/icons](trip_tracker/web/static/icons).
  When icon assets change, update the static icon cache-busting query in `layout.html` and
  `manifest.webmanifest`. Keep app logos, app names, manifest links, favicon links, and Apple touch
  icon links out of the login page.
- Do not change the active palette until the user chooses one. When a palette is applied, keep
  `styles.css`, the bundled nginx error pages, `theme-color` metadata, `manifest.webmanifest`, and
  the app icon visually coordinated.
- Keep palette changes high contrast and operational: preserve readable body text, visible form
  controls, and distinct warning, danger, success, and primary-action colors.

### Changelog Format
- `CHANGELOG.md` release headings use unbracketed version labels and `MM.DD.YYYY` release dates,
  such as `## 1.2.4 - 07.02.2026`.
- Keep the active development section as `## x.y.z - Unreleased` until that version is released.

---

## Common Tasks

### Adding a New Database Field
1. Create migration in `alembic/versions/` with timestamp:
   `docker compose run --rm ttapp alembic revision -m "description"`
2. Update SQLAlchemy model in [models.py](trip_tracker/models.py)
3. Validate through the Docker app image, for example
   `docker compose run --rm ttapp alembic upgrade head`
4. Migration auto-runs on Docker container startup

### Adding an API Endpoint
1. Add route to [api/routes.py](trip_tracker/api/routes.py)
2. Use `Depends(get_db)` for database session
3. Leave the default API bearer-token middleware in place for non-OwnTracks API routes, and update
   the explicit exemption list in [api/deps.py](trip_tracker/api/deps.py) only for intentional
   health-check or OwnTracks-ingestion endpoints.
4. Return JSON or raise `HTTPException`

### OwnTracks Ingestion During Database Outages
- OwnTracks ingestion is HTTP-only. Keep `/api/owntracks`, `/api/owntracks/`, `/api/pub`, and
  `/api/pub/` independent from the normal `Depends(get_db)` dependency so database failures can be
  translated into a controlled API response.
- After authentication, decryption, and validation, verify Alembic migrations and attempt the
  PostgreSQL write. Return `200 []` only after the commit succeeds. Return `503`,
  `Retry-After: 30`, and `Cache-Control: no-store` when PostgreSQL or migrations are unavailable.
- The OwnTracks mobile app is the only outage queue. Do not add a server-side SQLite queue, replay
  worker, MQTT subscriber, or queue storage volume.
- Preserve retry idempotency: an exact resent HTTP event must not create a second raw event row.
- The limp-mode page is the only browser-facing database-outage page. It is intentionally
  responsive for desktop and mobile and returns HTTP 200 so the bundled nginx browser error pages
  do not replace it. Non-OwnTracks API routes should return a 503 JSON response while the database
  is unavailable.

### Adding a Web Page
1. Create Jinja2 template in `web/templates/`
2. Add route to [web/routes.py](trip_tracker/web/routes.py)
3. Use `authenticate_web_credentials` if page should require login
4. Pass context dict to `templates.TemplateResponse()`

### Trips Page Editing Boundaries
- Existing trip rows display trip dates and odometers as read-only values. Row update forms accept
  selected origin/destination waypoint IDs from dropdowns plus mileage edits; posted dates,
  free-text names, and odometer fields are not accepted for existing rows.
- A distance edit keeps the edited trip's existing start odometer and recalculates only that trip
  and later trips in the same start month. Do not change earlier trips or any other month, and never
  change another trip's stored distance.
- The Work Trips page month selector is a single browser month/year picker. It defaults to the app's
  current `LOCAL_TIMEZONE` month, auto-loads the selected month, and displays the month as
  `Showing June 2026 (06/2026)` style text under the Work Trips title.
- The Work Trips page shows compact selected-month summary cards below the month selector line.
  Keep the cards scoped to the selected month: work trips plus non-work trips, work trips only,
  OwnTracks events, work trip count, reimbursement, and monthly average gas. Use comma thousands
  separators for large displayed summary totals while keeping form input values unformatted.
- The Monthly Work Trips list appears above Add Work Trip, and Add Work Trip appears above the
  extra report expenses card.
- The Trips root route renders a lightweight loading shell first. The selected-month cards,
  Add Work Trip form, work trip rows, extra report expense rows, and deleted-trip rows render
  through `/trips/content`, which is fetched by the shell so direct Work Trips loads show a loading
  message before month calculations finish.
- Work Trips is the only authenticated page that keeps its page title, description, and header
  divider. Keep the title and selected-month description on the left and the month selector and PDF
  download controls on the right in one compact, bottom-aligned desktop row directly above the
  divider. Collapse the two groups vertically on narrow screens instead of squeezing or clipping
  them.
- The Work Trips extra report expenses card sits above Deleted Work Trip Records. It accepts a
  date, expense reason, and price, enforces a hard cap of five expenses per report month, and
  includes those expenses as the final PDF table entries after trip rows.
- Manual trip creation defaults the date field to the app's `LOCAL_TIMEZONE` current date and uses
  origin/destination waypoint dropdowns populated from saved waypoints. Manual inserts calculate
  and save start/end odometers immediately from the current rolling OwnTracks odometer checkpoint,
  then resequence that trip and all later trips when the inserted date is before existing trip rows.
  New manual trips are placed after existing trips on the selected local date, and resequencing keeps
  existing positive odometer gaps between trips so non-trip driving remains represented. Monthly
  Work Trips rows use distinct full-row tinting without changing the table layout: unedited
  OwnTracks-generated rows use subtle blue, edited non-manual rows use purple, and only trips
  created from the Add Work Trip form use subtle gold. Do not add a separate Edited pill. Keep the
  three-color explanation key directly below the Monthly Work Trips list. Deleted Work Trip Records
  continue using creation-source tinting so true manual deleted records and automatic deleted
  records remain visually distinct.
- Dashboard work trip plus non-work trip distance cards use OwnTracks path distance as the
  total-distance source but floor the combined total at the stored work trip total after
  one-decimal rounding, so the displayed non-work trip remainder is never negative.
- Dashboard OwnTracks Events count is scoped to the current app-local month. The Work Trips count
  card shows app-local Today, Week, and Month counts inside the same card; Week uses a
  Monday-Sunday local week. Keep those three Work Trips counts in one row on mobile when they fit
  without clipping, with values aligned on the same row. The month starts at midnight on the first
  day in `LOCAL_TIMEZONE` (default
  America/Detroit), and month rollover must not delete prior-month trips, OwnTracks rows, gas price
  records, or derived app data. Monthly OwnTracks summary rollups preserve selected-month web totals
  and event counts after raw OwnTracks location/event rows are purged. Dashboard summary cards use
  comma thousands separators for large displayed totals.
- The Dashboard current-month reimbursement card must use the same monthly trip miles,
  reimbursement gallons, monthly gas price, `VEHICLE_MPG`, and manual extra expense total as the
  PDF report. Display the card's reimbursement gallons at one decimal place.
- The Dashboard home content shows the Location State card as the first visible card before other
  stat cards and distance summary cards. On full-width layouts, keep Dashboard top statistic cards
  and distance summary cards compact like the Work Trips selected-month cards, with each row still
  spanning the app width. Mobile should continue stacking those cards one per row. Do not add a
  separate Dashboard page title, description, or divider above the cards; keep app-local time in a
  footer below the Recent Work Trips panel.
- Authenticated Waypoints and Diagnostics pages start directly with their functional content and
  do not render separate page titles, descriptions, or header dividers. Keep the OwnTracks waypoint
  export action in a footer below the saved-waypoint list.
- The shared top bar uses a transparent brand logo plus centered blue raised navigation buttons with icons and labels on
  authenticated desktop pages. Show the current app version as a small readable line directly under
  the Trip Tracker brand title. On mobile, hide the brand/icon and keep the blue navigation
  buttons as icon-only controls in one full-width top-bar row. App buttons and button-style links
  are raised, brighten on hover, and press inward when clicked while preserving non-navigation
  button colors. Avoid fixed bottom navigation, and use a normal non-edge-to-edge viewport plus
  standalone/browser manifest fallback so phone system navigation remains visible. The login page
  has no shared top navigation. The brand icon/text is display-only and must not be a clickable
  home link.
- The Dashboard root route renders a lightweight loading shell first. The expensive Dashboard
  summary queries render through `/dashboard/content`, which is fetched by the shell so direct
  homepage loads show a loading message before calculations finish.
- During PostgreSQL outages, full-page Dashboard and Work Trips loads should render the limp-mode
  warning page instead of the loading shell. Content fetches such as `/dashboard/content` and
  `/trips/content` must return only the limp-mode panel fragment so the shell does not nest a
  second top bar; shell JavaScript should redirect to `/` when a limp-mode fragment is
  detected so already-loaded pages do not keep stale navigation visible. The full outage page is
  end-user facing, uses the `Service Temporarily Unavailable` heading, hides all shared app chrome
  and navigation, avoids host/IP/connection-string details and database status cards, and keeps
  retrying `/` so the normal app/login flow resumes when service returns. Fetched panel fragments must
  not include the retry script. Do not show retired server-side queue status on the outage page.

### Debugging Trip Generation
1. Check `/diagnostics` page for OwnTracks state and recent events.
2. View Compose logs with `docker compose logs -f ttapp`; use
   `docker service logs -f <stack>_ttapp` for Swarm.
3. All runtime, request, worker, trip-calculation, and debug logs go to stdout/stderr only. Do not
   add file handlers or in-app application-log viewers/downloads.
4. Successful and failed web login attempts are stored in `web_login_audits` and shown on
   `/diagnostics` in separate compact tables. Password values are never stored.
5. Diagnostics can hide individual failed-login rows from the UI while preserving the database
   audit row and JSON Lines export. When Cloudflare IP blocking is enabled and configured, Diagnostics can block/unblock
   failed-login IPs and manually entered valid IPs through app-managed Cloudflare zone IP Access
   Rules. Manual blocks require a reason, automatic blocks record the failed-login threshold
   reason, and the app-managed blocked-IP list shows each reason with an Auto or Manual pill plus a
   remove button that deletes the Cloudflare rule and local row. Automatic blocking occurs after
   the configured consecutive failed-login threshold and successful login resets that IP's
   consecutive count. Diagnostics paginates successful-login rows, failed-login rows, and
   app-managed Cloudflare blocks in compact 10-row pages; successful-login rows show a Password or
   Passkey method pill instead of an account column. On mobile, pagination keeps First, Previous,
   Next, and Last in one full-width row with the page count as text below. Pagination controls
   should progressively update only the active list and preserve the current scroll position while
   keeping normal links as a fallback. Failed-login row block buttons must use the failed-login
   table's corrected effective client IP.
6. Diagnostics includes a Configure Passkey card for the single configured web user. Passkey
   creation requires an authenticated web session, lists configured passkeys, and removes only the
   selected local credential row. Passkey login failures must stay on the same failed-login audit,
   lockout, and Cloudflare auto-block path as password login failures.
7. Use Diagnostics `Download Full Backup` before destructive deployment or database work. The
   backup/restore card is at the bottom of the page, and the manual full-backup
   download control sits with the lower upload-restore controls. Restore replaces all app table
   data from a validated `.json.gz` backup and is enabled only when web login is configured.
   Diagnostics also lists retained automatic backups from `AUTOMATIC_BACKUP_DIR`; each retained
   backup can be downloaded individually, startup-created files are labeled, and the selected file
   can be restored after typed `RESTORE` confirmation.
8. Diagnostics groups the top cards together in this order: Application, System Status, Data,
   Latest Records, OwnTracks State, Manual Odometer, EIA API, Configure Passkey, and Hard Drive
   Space. Keep the group at three cards per row on desktop and one card per row on mobile. The
   Diagnostics page shows a yellow or red app-health banner above the top cards when monitored
   checks are degraded or unavailable. The banner and Pushover notifications must use the shared
   `app_health.py` snapshot so they stay consistent. The
   System Status card shows PostgreSQL availability, local/remote placement, latency with a
   green/yellow/red status dot based on the app-health database latency thresholds, database size,
   total app-record count, and pool/timeout details. Keep that visible latency indicator immediate;
   only Pushover latency alerts use the sustained-duration confirmation. App-health disk warnings
   and critical issues use configured free-space MiB thresholds rather than used percentages. The
   Data card shows raw record counts plus lowest,
   current, current-month average, and highest gas price readings; format large displayed counts
   with comma thousands separators, keep the low/high values based on raw gas price
   snapshots, and keep the monthly average based on the current app-local month. The detailed
   OwnTracks state-change log and recent OwnTracks database entries are paginated in compact 10-row
   pages with the same mobile full-width pagination row used by the login and Cloudflare block
   lists. These paginated lists should update in place without a full-page refresh when JavaScript
   is available.
   The recent OwnTracks entries table shows original event time, capture-to-receive delay, and
   readable event labels instead of the database row ID, raw receive timestamps, or battery level.
   The OwnTracks state-change log intentionally omits per-section distance and shows original event
   time, received delay, state, waypoint, source, elapsed duration since the prior state change,
   and the event row's rolling odometer when available.
9. Diagnostics shows the app version in the Application card, shows hard drive space for key
   runtime paths, combines paths into one row when exact used bytes and total bytes match, and
   includes current database size plus total app record count at the bottom of the card.
10. Trip calculation details logged to `trip_tracker.trip_calculation` logger

---

## Testing Patterns

**Test Files**: Tests are in `tests/` with names like `test_mileage.py`, `test_owntracks.py`

Key test modules:
- `test_mileage.py` - Trip generation logic, odometer calculations
- `test_owntracks.py` - Payload parsing and event handling
- `test_pdf.py` - Report generation
- `test_timezone.py` - Timezone conversions
- `test_web.py` - Web UI routes

**Database Testing**: Tests use SQLite in-memory database by default. Check fixture setup in test files.

---

## Deployment

See [INSTALL.md](INSTALL.md) for complete Docker and Portainer setup guide.

**Key Points**:
- Requires Docker Engine and Docker Compose v2
- Uses `docker-compose.yml` with `ttapp`, `ttnginx`, cloudflared, and an optional default-on `postgres`
  service behind the `local-postgres` Compose profile.
- Docker Swarm deployments use `docker-stack.yml`; add `docker-stack.local-postgres.yml` only when
  bundled PostgreSQL should run in Swarm. Swarm stack files must avoid Compose-only `build`,
  `profiles`, conditional `depends_on`, and loopback-only port binding assumptions. Use prebuilt
  `APP_IMAGE` and `NGINX_IMAGE` tags, and configure Cloudflare Tunnel to target `http://ttnginx`
  over the stack overlay network.
- Keep the Compose and Swarm service keys uniquely named `ttapp` and `ttnginx`. The nginx upstream
  must resolve `ttapp:8000`, and all Swarm services, including optional bundled PostgreSQL, must
  share the `trip-tracker-internal` overlay network. Do not rename the existing deployment variables
  `APP_IMAGE`, `NGINX_IMAGE`, `APP_UID`, `APP_GID`, `HOST_DATA_DIR`, or `HOST_BACKUP_DIR` with
  these service keys.
- Keep the Swarm `cloudflared` service at two replicas with `max_replicas_per_node: 1`, a
  five-second restart delay, and start-first updates unless the deployment architecture changes.
  Both replicas intentionally use the same `CLOUDFLARED_TUNNEL_TOKEN`.
- `.github/workflows/publish-swarm-images.yml` publishes app and nginx images to GHCR on relevant
  `main` changes. Keep the package-version tag, `latest`, and immutable full-commit-SHA tag aligned,
  and keep `.env.docker.example`, README, and INSTALL examples on the current released version.
- The Swarm `ttapp` task runs with configurable `APP_UID`/`APP_GID` defaults of `1000:100`. Its
  shared `HOST_DATA_DIR` and `HOST_BACKUP_DIR` must already be writable by that identity because a
  non-root Swarm task does not run the entrypoint's root-only path ownership preparation.
  Run `scripts/prepare_host_directories.sh` on every eligible host to create either missing bind
  path and its parents with `mkdir -p`, then verify ownership for that configured identity.
- The bundled `postgres` service remains the default database target when
  `COMPOSE_PROFILES=local-postgres`, but app startup and migrations wait on the configured
  `DATABASE_URL` instead of depending on the bundled local database container's health. For a
  central network PostgreSQL server, set `COMPOSE_PROFILES=` and point `DATABASE_URL` at that
  server; `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` then only matter if the local
  PostgreSQL profile is enabled again. Invalid or unparseable `DATABASE_URL` values must not crash
  app import; they should be classified as database unavailable so outage mode can start while the
  environment value is corrected.
- Runtime PostgreSQL connections use `pool_pre_ping` plus configurable pool size, overflow,
  timeout, recycle, and connect-timeout settings so remote database connections are reused and
  stale network connections are replaced safely.
- Docker publishes the web service on `127.0.0.1:${HTTP_PORT:-80}`. The bundled `cloudflared` service uses
  host networking so Cloudflare Tunnel can target the loopback listener, such as
  `http://127.0.0.1:2082` when `HTTP_PORT=2082`.
- When `COMPOSE_PROFILES=local-postgres`, PostgreSQL data is stored in the volume selected by
  `POSTGRES_DATA_VOLUME`, defaulting to `trip-tracker-postgres-data`. Keep the explicit name stable
  across Compose/Portainer project-name changes. Do not use `docker compose down -v`, prune
  volumes, or change the volume name unless you have a verified backup and migration plan. Remote
  PostgreSQL deployments must be backed up and maintained on the central database server.
- Environment variables in `.env` control all configuration. Production Docker must have
  `SECRET_KEY`, `WEB_LOGIN_USERNAME`, and `WEB_LOGIN_PASSWORD` set; the app fails closed when
  production login credentials are missing or the session secret is still `change-me`. When web
  login is enabled in any environment, change `SECRET_KEY` from the default.
- Migrations run automatically on app startup
- If PostgreSQL is unavailable at startup, Docker starts the app in outage mode instead of exiting.
  Web pages show the service-unavailable page and OwnTracks HTTP requests receive retryable `503`
  responses until PostgreSQL and migrations are ready. Keep `APP_HEALTHCHECK_START_PERIOD` longer than
  `DB_WAIT_TIMEOUT_SECONDS` so Swarm does not replace the app task while the entrypoint is waiting
  before limp mode starts.
- Daily gas snapshots run as an app-container background scheduler; there is no separate
  `gas-snapshot` Compose service.
- Diagnostics page available at `http://server/diagnostics`
- Public web service exposes rendered web pages and OwnTracks ingestion only; `/api/health`, admin API
  routes, `/docs`, `/redoc`, and `/openapi.json` are intentionally not internet-facing.
- Public web service serves custom, unbranded end-user error pages from `deploy/nginx/error-pages/` for
  common 4xx and 5xx responses. Keep the pages visually matched, include a `/login` link that can
  switch to home for authenticated browsers. Browser/static proxy locations intercept upstream app
  errors so missing page URLs show those custom pages; OwnTracks API proxy locations do not
  intercept errors, so API clients keep JSON responses.
- Public web service passes Cloudflare's `CF-Connecting-IP` through to the app when present. The app uses
  that effective client IP for login lockouts, login audit rows, and Cloudflare auto-blocks.
- Passkey login derives its WebAuthn origin from `PASSKEY_ORIGIN`, the browser `Origin` header, or
  trusted reverse-proxy scheme/host headers. For public Cloudflare Tunnel deployments, verify the
  browser origin is the public HTTPS URL or set `PASSKEY_ORIGIN` and `PASSKEY_RP_ID` explicitly.
- Diagnostics includes authenticated full data backup and restore controls for app database rows
  and saved OwnTracks waypoint export. Automatic 6-hour backups are stored under
  `AUTOMATIC_BACKUP_DIR`, defaulting to `/data/backups` on the dedicated `HOST_BACKUP_DIR` bind
  mount; treat backup files as sensitive location history. Retained automatic backups can be
  downloaded individually from Diagnostics after web login. Shared-storage failures must retry
  until a backup succeeds instead of waiting for the next 6-hour pass.
- Persistent backups are host bind-mounted through `HOST_BACKUP_DIR`; app-health state uses
  `HOST_DATA_DIR`. Runtime logging remains console-only and login audits remain in PostgreSQL.
- Optional Pushover app-health notifications use `PUSHOVER_ENABLED=true`, a Pushover app API token
  in `PUSHOVER_TOKEN` or `PUSHOVER_APP_KEY`, and a user/group key in `PUSHOVER_USER` or
  `PUSHOVER_USER_KEY`. High database latency must remain elevated for the configured sustained
  period before it enters a Pushover notification; Diagnostics continues showing the immediate
  latency reading. Disk health uses adjustable free-space warning and critical thresholds instead
  of used percentages. The app sends degraded/unavailable notifications on monitored state
  changes and one restored notification when all monitored checks are healthy again.
  Unchanged degraded or unavailable states repeat after
  `APP_HEALTH_REMINDER_INTERVAL_SECONDS`, which defaults to one hour.

---

## Common Pitfalls

1. **Timezone Confusion**: The server can run on UTC, but trip dates and day boundaries use `LOCAL_TIMEZONE`. Always convert with `datetime_to_local()` before displaying.

2. **Trip Dwell Time**: If waypoint transitions arrive too quickly, the trip won't be confirmed. The default is 5 minutes. Check `OWNTRACKS_WAYPOINT_DWELL_MINUTES`, OwnTracks event timestamps, whether the arrival coordinates start inside the saved waypoint radius, and whether later coordinates or waypoint state confirm the visit. A same-waypoint `leave` after the dwell window confirms an inside-radius arrival and can also confirm an OwnTracks-named outside-radius arrival, but an early leave, early next-waypoint arrival, or clearly-away movement before the dwell window rejects it. OwnTracks region labels alone are not enough without later state confirmation.

3. **Mileage Priority**: OwnTracks path distance is preferred, but if location updates are sparse, fallback to waypoint distance. Odometer values are never a distance source; manual distance edits override generated calculations. Prior trip end odometers are not the source for new generated trip starts.

4. **Odometer Precision**: Values stored and displayed as 0.1 mile precision. Manual entries are quantized during update.

5. **Data Retention**: Only raw OwnTracks location/event records are purged automatically, and only
   after at least 90 days even when `OWNTRACKS_LOCATION_RETENTION_DAYS` is set lower. Trips,
   odometers, reports, gas prices, monthly OwnTracks summary rollups, backups, and other derived
   app data are kept. Set `OWNTRACKS_PURGE_ENABLED=false` to disable raw OwnTracks cleanup.

6. **OwnTracks Retry Ordering**: OwnTracks retains failed HTTP messages on the device. Keep event
   timestamps authoritative and preserve exact-retry deduplication when changing ingestion.

7. **Remote Database URLs**: SQLAlchemy database URLs require URL-encoded passwords when reserved
   characters are present. For example, encode `@` as `%40`, `:` as `%3A`, `/` as `%2F`, and `%` as
   `%25` before placing the password in `DATABASE_URL`.

---

## AI Agent Skills

These specialized guides help AI agents with common development tasks:

- **[SKILL-trip-processor.md](.vscode/SKILL-trip-processor.md)** — Automatic trip generation, event sequences, odometer checkpoint, debugging trip detection
- **[SKILL-database-migrations.md](.vscode/SKILL-database-migrations.md)** — Adding database fields, creating Alembic migrations, schema changes, rollback patterns
- **[SKILL-mileage-calculation.md](.vscode/SKILL-mileage-calculation.md)** — Mileage priority system, Haversine distance, odometer estimation, trip editing, resequencing logic
- **[SKILL-api-and-web-routes.md](.vscode/SKILL-api-and-web-routes.md)** — Adding API endpoints, web pages, form handling, authentication, Jinja2 templates

---

## Documentation Links

- [README.md](README.md) — Project overview, setup, OwnTracks configuration, workflow
- [INSTALL.md](INSTALL.md) — Docker, Ubuntu, Portainer installation guide
- [CHANGELOG.md](CHANGELOG.md) — Release history and breaking changes
- [pyproject.toml](pyproject.toml) — Dependencies and build configuration
