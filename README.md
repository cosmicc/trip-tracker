# Trip Tracker

Trip Tracker receives OwnTracks waypoint events from an Android phone over HTTP,
stores them in PostgreSQL, lets you review and edit generated waypoint work trips, and produces
monthly mileage and expense PDF logs.

Upgrading an existing deployment from 1.4.4 or earlier requires coordinated database, storage,
service, and image-name changes. Follow [UPGRADE-1.5.md](UPGRADE-1.5.md) before deploying 1.5.0.

## Current Scope

- FastAPI web app with server-rendered review screens.
- Dashboard opens with a loading message before fetching calculated mileage and reimbursement
  summaries, starts the loaded page with its summary cards, and shows app-local time at the bottom.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/api/pub`.
- Retry-safe HTTP ingestion that returns `503 Service Unavailable` while PostgreSQL is unavailable
  so the OwnTracks mobile app retains and resends its own queued messages after recovery.
- OwnTracks waypoint transition model used to turn leave/enter events into work trips, with
  location updates between those events used as the primary trip distance.
- Manual current-odometer entry from the Diagnostics page, with the Manual Odometer card showing
  the latest current reading before saving a new checkpoint.
- Optional Pushover app-health notifications for degraded or unavailable app state and restoration.
- Manual trip entry defaults to today's local date and uses saved waypoint dropdowns for From/To,
  with trip-row waypoint and mileage review on the Work Trips page. Work Trips uses one month/year
  picker that loads the selected month automatically, keeps its title, selected-month line, and
  report controls in one compact desktop row above the divider, and shows compact selected-month
  summary cards.
- Waypoints starts with the saved-waypoint list and keeps the OwnTracks waypoint export action at
  the bottom of the page.
- Monthly gas price cache with a provider layer.
- Monthly PDF report generation with optional extra expense lines.
- GitHub Actions CI for linting and tests.

## Fuel Price Policy

The reimbursement formula is:

```text
monthly trip miles / vehicle MPG = reimbursement gallons
reimbursement gallons * Michigan monthly average gas price = total reimbursement
```

The first provider can fetch the current AAA Michigan regular gasoline average and store daily
snapshots. Monthly reports use a saved manual monthly average when present, or the average of
stored daily snapshots for that month. Historical Michigan monthly pricing sources can be added
behind `trip_tracker.services.gas_prices.GasPriceProvider` without changing report generation.
Set `VEHICLE_MPG` to the fuel economy that should be used for reimbursement calculations.

EIA support is scaffolded because the official API requires an API key and the exact series should
be configured with `EIA_SERIES_ID` once the preferred Michigan data series is selected.

## Docker Deployment

Trip Tracker is intended to run as a Docker Compose stack. It runs the complete stack:

- Optional bundled PostgreSQL database, or a remote PostgreSQL database through `DATABASE_URL`.
- FastAPI mileage app.
- Web service reverse proxy on port `80`.
- Daily gas price snapshot scheduler inside the app container.
- Cloudflare Tunnel connector using the configured tunnel token.
- Persistent Docker volume for database data, plus separate host bind mounts for backups and
  health state.
- In-app diagnostics page for database-backed successful and failed web-login audit records and
  OwnTracks state in the configured local timezone. The page starts directly with its operational
  content instead of a separate title block, and the top Diagnostics cards
  are grouped into a three-column desktop grid, hard drive rows combine matching used and total
  space readings, database latency includes a green/yellow/red status dot, a yellow or red degraded
  banner appears when monitored app-health checks need attention, detailed lists use compact 10-row
  pages, and Full Data Backup stays at the bottom.
- Dashboard Work Trips counts for today, the current Monday-Sunday week, and the current month.
- Authenticated desktop navigation shows the current app version under the Trip Tracker title and
  uses centered blue raised icon-and-label buttons matching the mobile web-app layout, where
  navigation becomes icon-only in one full-width top-bar row and leaves the bottom safe area clear
  for phone system navigation without opting into edge-to-edge phone drawing. App buttons use a
  raised treatment, brighten on hover, and press inward when clicked. The login page does not show
  the shared top navigation.
- Console-only app logging for Docker Compose and Docker Swarm log collection.
- Optional web UI IP allowlist while keeping only the OwnTracks ingestion API public.

Create a production `.env` with generated passwords:

```bash
./scripts/init_docker_env.sh
```

Review `.env`, set `CLOUDFLARED_TUNNEL_TOKEN` to the token from the Cloudflare dashboard, then
start the stack:

```bash
docker compose up -d --build
```

`scripts/init_docker_env.sh` uses the reusable host preparation script to create both bind-mount
directories with `mkdir -p`. Run it directly after changing either host path, or use `sudo` when
your account cannot create the configured directories:

```bash
sudo ./scripts/prepare_host_directories.sh .env
```

```bash
sudo rmdir /var/log/trip-tracker-login-failures.log
```

Useful commands:

```bash
docker compose ps
docker compose logs -f ttapp
docker compose logs -f ttnginx
docker compose down
```

With the default `COMPOSE_PROFILES=local-postgres` setting, database rows live in the Docker named
volume `postgres_data`, mounted at `/var/lib/postgresql/data` inside the PostgreSQL container.
Normal rebuilds such as `docker compose up -d --build` keep that volume. Do not use
`docker compose down -v`, Docker volume prune, or a different Compose/Portainer stack name unless
you have a verified backup and intend to move or recreate the local database.
To use a central PostgreSQL server instead, set `COMPOSE_PROFILES=` and point `DATABASE_URL` at
that server. The bundled `postgres` service is then not deployed, and the `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD` variables only matter if you enable the local PostgreSQL
profile again. The app startup and migrations always wait on the configured `DATABASE_URL`. If the
database password contains URL-reserved characters, encode it before adding it to `DATABASE_URL`.
For example, `@` becomes `%40`.

If PostgreSQL is unreachable when the container starts, the app starts in outage mode instead of
exiting. Browser pages show a responsive service-unavailable page, non-OwnTracks API routes return
`503` JSON, and OwnTracks HTTP ingestion returns a fast retryable `503` with `Retry-After: 30`.
OwnTracks retains unsuccessful HTTP messages on the phone and resends them after PostgreSQL and
database migrations are ready. Exact HTTP retries are accepted without inserting a duplicate raw
event. Automatic trip processing, gas snapshots, and automatic backups pause their database-writing
passes while PostgreSQL is unreachable. The outage page hides normal app navigation and connection
details and retries the app home page until service returns. A malformed `DATABASE_URL` is treated
as database unavailable so the outage page can still start while the setting is corrected.
`APP_HEALTHCHECK_START_PERIOD` should stay longer than
`DB_WAIT_TIMEOUT_SECONDS`; this prevents Docker Swarm from replacing the app task while the
entrypoint is waiting before limp mode starts.

Docker Swarm deployments use [docker-stack.yml](docker-stack.yml) instead of `docker-compose.yml`.
Swarm cannot build images, use Compose profiles, or keep the normal Compose loopback-only port
binding. The `Build and publish Swarm images` GitHub workflow publishes versioned, `latest`, and
commit-SHA app and nginx images to GHCR. Set `APP_IMAGE` to
`ghcr.io/cosmicc/trip-tracker-app:1.5.0` and `NGINX_IMAGE` to
`ghcr.io/cosmicc/trip-tracker-nginx:1.5.0` through Portainer or the shell, and deploy the base
stack for remote PostgreSQL. Add
[docker-stack.local-postgres.yml](docker-stack.local-postgres.yml) only when the bundled
PostgreSQL service should be part of the Swarm stack. In Swarm, configure the Cloudflare Tunnel
origin service as `http://ttnginx` so cloudflared reaches the uniquely named `ttnginx` service over
the stack's `trip-tracker-internal` overlay network. The Swarm `ttapp` task defaults to `APP_UID=1000`
and `APP_GID=100`; make the shared `HOST_DATA_DIR` and `HOST_BACKUP_DIR` writable by that identity
on every eligible node.
Existing 1.4.4 Portainer deployments must update the Cloudflare Tunnel origin from `http://mlnginx`
to `http://ttnginx` when applying the service rename. Continue using `APP_IMAGE`, `NGINX_IMAGE`,
`APP_UID`, `APP_GID`, and `HOST_DATA_DIR`; v1.4.1 adds `HOST_BACKUP_DIR` for the dedicated backup
mount.
Swarm runs two `cloudflared` replicas and limits them to one replica per node, providing redundant
Tunnel connectors without making the stateful application service active-active.

OwnTracks HTTP mode should point at:

```text
http://your-server/api/owntracks
```

Use the `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` values from `.env` for
OwnTracks HTTP Basic Auth, and set the OwnTracks payload encryption key to the
`OWNTRACKS_ENCRYPTION_KEY` value from `.env`. If you put credentials directly in the URL, use:

```text
http://owntracks:password@your-server/api/owntracks
```

For internet-facing use, put TLS in front of this stack or extend the web service container
with certificates so OwnTracks sends location data over HTTPS.

Non-OwnTracks API routes require a separate key:

```bash
curl -H "Authorization: Bearer ${WEB_API_KEY}" \
  "http://127.0.0.1:${HTTP_PORT:-80}/api/locations"
```

Do not reuse `OWNTRACKS_ENCRYPTION_KEY` as `WEB_API_KEY`. `/api/health` stays unauthenticated for
internal container health checks.

To restrict the browser UI while leaving OwnTracks ingestion open, set `WEB_ALLOWED_CIDRS`
to comma-separated IP blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

When this is blank, the web UI is open to all clients. When set, only
`POST /api/owntracks`, `POST /api/owntracks/`, `POST /api/pub`, and `POST /api/pub/` stay
reachable from any IP for OwnTracks. Pages such as `/`, `/trips`, `/waypoints`, `/diagnostics`,
and `/static/` require a matching client IP. Other `/api/` routes, `/docs`, `/redoc`, and
`/openapi.json` are blocked at the public web service reverse proxy.

The web service serves matching, end-user-focused error pages for common browser and gateway errors: 400,
401, 403, 404, 405, 408, 413, 429, 500, 502, 503, and 504. Each page explains the error and links
to `/login`, or back home when the browser already has an authenticated session.

To require a simple username/password login for browser pages, set both web login variables:

```env
SECRET_KEY=generate-a-long-random-value
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
PASSKEY_RP_NAME=Trip Tracker
PASSKEY_RP_ID=
PASSKEY_ORIGIN=
```

The login protects rendered web pages such as `/`, `/trips`, `/waypoints`, and `/diagnostics`.
Unauthenticated browser paths are limited to `/login`, passkey login challenge/verify endpoints,
root icon and manifest files, the service worker, and `/static/` assets needed to render those
pages. Non-OwnTracks `/api/` routes use `WEB_API_KEY` instead of the web login, and the public web service
only exposes the OwnTracks ingestion endpoints. If you access the app over plain HTTP for local
testing, set
`WEB_SESSION_COOKIE_SECURE=false` so the browser accepts the session cookie. The login page does
not show the app name before authentication, keeps invalid username/password attempts on the form
with a top status-line error, and temporarily locks out repeated failed attempts.
`WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` must be set together. When web login is enabled,
`SECRET_KEY` must be changed from `change-me`; production Docker starts fail closed if the login
credentials or session secret are missing. Docker publishes the web service only on `127.0.0.1`, so public
access should come through the bundled Cloudflare Tunnel service.
Each successful login, failed login attempt, and lockout rejection is stored in PostgreSQL as a
structured audit record. Failed entries include client IP
details, submitted username, password length, user agent, request path, reason, attempt count,
lockout state, and timestamps. Successful entries include client IP details, submitted username,
authentication method, web client, request path, and timestamps. The raw submitted password is
never stored. Diagnostics uses the stored effective client IP for successful-login and failed-login
rows, and the failed-login block button targets that same IP.

The login form also offers `This is a public device`, off by default. Public-device sessions
disable Device Sign-In, expire after 15 minutes without browser activity, do not register the web
app service worker, and clear the session cookie, browser cache, and site storage at timeout or
logout. Unchecking the option immediately restores Device Sign-In. Hover over or keyboard-focus the
public-device option to see this explanation on the login page.

Diagnostics includes a Configure Passkey card for the single configured web-login user. Sign in
with the normal username/password once, create a passkey from Diagnostics, then use Device Sign-In
on the login page. Passkeys require a secure browser origin; when the app is published through
Cloudflare Tunnel, the default Docker web service config forwards the public HTTPS origin. If your proxy
setup is unusual, set `PASSKEY_ORIGIN=https://your-host.example.com` and
`PASSKEY_RP_ID=your-host.example.com` explicitly.

See [INSTALL.md](INSTALL.md) for the full Docker and Portainer installation guide.

## OwnTracks HTTP Setup

Set OwnTracks HTTP mode to:

```text
https://your-host.example.com/api/owntracks
```

Set HTTP Basic Auth in OwnTracks from `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD`, then set the
OwnTracks payload encryption key to `OWNTRACKS_ENCRYPTION_KEY`. When the encryption key is
configured, plaintext OwnTracks HTTP payloads are rejected.

`OWNTRACKS_ENCRYPTION_KEY` must be 32 UTF-8 bytes or fewer. The app pads shorter keys to
libsodium's 32-byte SecretBox key size, matching OwnTracks Recorder behavior.

The `/api/pub` alias, including `/api/pub/`, is also available for Recorder-style setups.

OwnTracks waypoints are saved as read-only work waypoints. When `OWNTRACKS_SYNC_WAYPOINTS=true`,
published OwnTracks waypoint payloads create or update matching app waypoints. The web app can
export the saved list as OwnTracks waypoint JSON for backup/import.

## Full Data Backup And Restore

Diagnostics includes a full app data backup and restore panel at the bottom of the page when
`WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` are configured. The manual
`Download Full Backup` action sits with the lower upload-restore controls and creates a `.json.gz`
file containing all Trip Tracker database tables plus an OwnTracks waypoint export. Treat this
file as sensitive location history.

The app also creates automatic full-data backups every 6 hours when
`AUTOMATIC_BACKUPS_ENABLED=true`, which is the default. Automatic backups are stored in
`AUTOMATIC_BACKUP_DIR`, defaulting to `/data/backups` in Docker. That container path uses the
dedicated `HOST_BACKUP_DIR` bind mount, such as `trip-tracker/backups` on shared Swarm storage.
If shared storage is unavailable, including a stale file handle failure, backup creation pauses
and retries every `AUTOMATIC_BACKUP_RETRY_SECONDS` until one succeeds; the normal six-hour schedule
then resumes.
Diagnostics lists retained automatic backups and can restore one after you type `RESTORE`. The
retention policy keeps the newest 4 recent automatic backups plus one daily backup for each of the
prior 2 days. Startup-created and emergency-rebuild safety backups are labeled in the table. Each
listed automatic backup also has its own download button. Backup downloads use
`Cache-Control: no-store` and
require the same web login as restore because the files contain location history.

To restore, upload the same backup file on Diagnostics and type `RESTORE`. Restore validates the
file first, then replaces the current app table rows in one transaction. Restore is a replace, not
a merge: matching existing rows are overwritten from the backup and should not create duplicates.
Uploaded restore files and retained automatic backup files are limited by
`MAX_BACKUP_RESTORE_BYTES`, default `262144000` bytes. In-app restore does not restore Docker
volumes, PostgreSQL users, passwords, or Docker-managed console log history.

## Trip Detection

The app generates work trips directly from OwnTracks waypoint transitions:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- The destination `enter` event must remain valid for at least
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES` minutes. The default is 5 minutes so driving through a
  waypoint does not create a trip.
- Dwell confirmation can come from later OwnTracks coordinates inside the waypoint radius, a later
  same-waypoint `leave` after the dwell window, a later next-waypoint `enter` after the dwell
  window for an inside-radius arrival, or the next processing pass after the dwell timer when no
  earlier event contradicts an inside-radius arrival. If an OwnTracks-named arrival starts outside
  the saved radius, a later same-waypoint `leave` after the dwell window can still confirm the
  visit. OwnTracks region labels by themselves do not override coordinates outside the saved
  radius, and an early leave, early next-waypoint arrival, or clearly-away movement before the
  dwell window rejects the visit. A rejected waypoint visit cannot become the origin for the next
  return trip.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Work trips between the same non-home waypoint are kept only when the calculated distance is at
  least 1.0 mile, because shorter same-waypoint loops are treated as invalid GPS drift or
  non-work trips.
- If an `enter` event arrives without a matching `leave`, the app infers the most likely origin
  from the previous waypoint. If there is no previous waypoint and the destination is not `Home`,
  the app assumes the missed origin was `Home`.

Trip data is calculated automatically. Every incoming OwnTracks location or transition payload is
stored in `owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day when PostgreSQL is reachable. During a database outage, the endpoint returns
`503` without accepting the payload, leaving OwnTracks responsible for retrying it later.
When the app sees a qualifying trip, it writes the generated row to `trips`.
PostgreSQL enforces one automatic row for each exact source-event signature and rejects a second
automatic row with the same day, route, distance, and nonblank start/end odometer interval.
Before inserting, the app checks both signatures and reuses the existing automatic row so a
shifted duplicate transition pair cannot roll back the processing checkpoint or block later trips.
OwnTracks `tst` event time is the authoritative timestamp for trip dates and ordering; the server
receive time is kept separately for diagnostics because phone data can be buffered.
Trips that cross local midnight stay assigned to the day on which the trip started, and OwnTracks
path points are included through the destination arrival event even when that event is after
midnight.
The server can run on UTC; app day/month selection, dashboard time, and gas snapshot dates use
`LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

Generated mileage uses this order:

1. OwnTracks location path distance from the location updates received between the waypoint
   `leave` and `enter` events.
2. Waypoint-to-waypoint distance when OwnTracks path data is not available.

Keep OwnTracks location reporting enabled so the app can sum the actual path between waypoint
events. If a trip window has only transition events and no location updates between them, the app
falls back to waypoint distance. Odometer values are never used to calculate trip distance,
Dashboard work trip plus non-work trip totals, or monthly work trip plus non-work trip totals.
Generated work trips use the master rolling OwnTracks odometer checkpoint or stamped rolling
OwnTracks event odometers for display; they do not use the previous work trip end odometer as the
source for the next start.
Dashboard work trip plus non-work trip cards are floored at the stored work trip total after
one-decimal rounding, so the displayed combined total is never lower than the work-trips-only total
and the implied non-work trip remainder is never negative. Edit a work trip's saved waypoint route
or miles on the `Work Trips` page when generated values need correction. A distance correction
keeps the edited trip's start odometer and resequences only that trip and later trips in the same
start month. Earlier trips and trips in other months are not changed.
Manual work trips entered from the `Work Trips` page default to today's local
date, use saved waypoint dropdowns for From/To, and save start/end odometers immediately from the
current rolling OwnTracks odometer checkpoint plus the entered work trip miles. A manual work trip
is placed after the existing work trips on the selected local date, so backdated manual entries
land at the end of that day and today's manual entries become the latest work trip for today. If
the manual work trip is inserted before existing work trips, the app resequences that work trip and
every later work trip so odometers remain cumulative across month boundaries while preserving
existing positive odometer gaps between work trips for non-work trip driving. Work trip rows use
subtle blue for unedited OwnTracks-generated trips, purple for edited trips, and gold for trips
created from the Add Work Trip form. A color key below the list explains each row shade. Deleted
work-trip records continue using source-based shading for true manual entries and automatic
entries. Deleting a work trip from the `Work Trips` page also saves an
exact deleted-trip record so only that same OwnTracks transition pair is not generated again;
future work trips with the same route are still generated normally.
Automatic same-waypoint work trips under 1.0 mile are also removed with an exact suppression record
so older invalid rows do not return from the same OwnTracks transition pair.
The checkpoint odometer is advanced from OwnTracks path distance between received points even when
those points do not become a trip. Each processed OwnTracks location row stores the rolling
odometer value for that point, and generated trips use those rolling values for start odometers
when available. If transition rows are not stamped yet, generated trips use the master rolling
checkpoint before the trip start. Prior trip end odometers are not used as the source for generated
trip starts. The trip end odometer is always advanced from the start odometer by the stored trip
distance so the odometer display follows the trip miles. If a recently recorded work trip is
missing displayed odometers, automatic trip processing can backfill those blanks from the master
checkpoint when retained OwnTracks path rows support the estimate. Segments fully inside the same
saved waypoint are ignored to reduce stationary GPS drift. Normal manual odometer entries on
Diagnostics are accepted only while OwnTracks reports the exact `Home` waypoint; the normal Save
button is disabled elsewhere. A Home save resets the checkpoint and aligns all displayed work trip
odometers so the latest trip end matches the entered reading while preserving every trip's mileage
and existing positive odometer gaps between trips.
If those gaps are corrupted, the red Emergency Rebuild action remains available away from Home. It
first writes a full backup, never changes stored trip distances, treats the entered value as the
current master and latest trip odometer, reconstructs oversized gaps from retained OwnTracks data
when possible, discards gaps of 200 miles or more, and removes smaller gaps largest-first only when
needed to produce a valid best-effort sequence.
Trip creation, editing, deletion, resequencing, and missing-odometer repair never move the master
rolling odometer checkpoint. Only OwnTracks distance processing and an explicit manual odometer
entry can update the master rolling odometer.
Dashboard total-driven cards and the Work Trips selected-month cards sum OwnTracks coordinate
segments directly for the selected local day or month, so manual odometer resets do not affect work
trip plus non-work trip totals.
Dashboard OwnTracks Events and Work Trips count cards are scoped to the current app-local month,
which starts at midnight on the first day of the month in `LOCAL_TIMEZONE`. Prior months remain
available from the Work Trips month picker; month rollover does not delete prior-month trip,
OwnTracks, or gas price records. Before raw OwnTracks location/event rows age out, the app stores
monthly OwnTracks summary rollups so selected-month web totals and event counts remain stable after
the raw location rows are purged.
The Dashboard current-month reimbursement card uses the same trip-mile total, reimbursement
gallons, monthly gas price, `VEHICLE_MPG`, and extra expense total as the downloadable PDF report,
with displayed gallons limited to one decimal place. Dashboard top statistic and distance cards use
the same compact sizing as the Work Trips selected-month cards on full-width layouts, while mobile
keeps each card on its own row. Dashboard and Work Trips summary cards use comma thousands
separators for large displayed totals.
The Diagnostics Manual Odometer card shows the current reading and its source next to the form so
the existing checkpoint can be checked before entering a correction. The top Diagnostics cards are
grouped together in this order: Application, System Status, Data, Latest Records, OwnTracks State,
Manual Odometer, EIA API, Configure Passkey, and Hard Drive Space. On desktop they render three
cards per row. The System Status card shows PostgreSQL reachability, whether the configured
PostgreSQL host is remote, database latency with a green/yellow/red status indicator, database
size, total app records, and pool/timeout details. The Data card includes
lowest, current, current-month average, and highest gas price readings and comma-formatted large
record counts. Diagnostics also shows hard drive space for key runtime paths with used-space bars,
combining paths into one row when their exact used space and total capacity match, and includes
current database size plus total app record count at the bottom of the card. When app-health checks
detect degraded or unavailable service, Diagnostics shows a yellow or red banner above the top
cards for database, disk-space, login-lockout, or app-managed Cloudflare block issues.
Recent OwnTracks entries,
OwnTracks state changes, successful-login attempts, failed-login attempts, and app-managed
Cloudflare blocked IPs are displayed 10 rows at a time with mobile pagination buttons in one
full-width row and the page count shown as text below. Pagination buttons update only the active
list and keep the current page position when JavaScript is available, with normal links as a
fallback. Recent OwnTracks entries show original event time, received delay, and a readable event
label instead of the database row ID, raw receive timestamps, or battery level.
Successful-login attempts show Password or Passkey method pills instead of an account column. The
OwnTracks state-change list omits the per-segment distance column and shows original event time,
received delay, state, waypoint, source, duration, and rolling odometer when available.

## Cloudflare Tunnel

The Compose file includes `cloudflared` as a normal required service for a remotely managed
Cloudflare Tunnel. The `cloudflared` container uses host networking so it can reach the host-bound
web service listener. In the Cloudflare dashboard, publish the application route to the host listener:

```text
http://127.0.0.1:80
```

If `HTTP_PORT=2082`, use `http://127.0.0.1:2082` as the Cloudflare Tunnel service URL. The Compose
stack always publishes the web service on `127.0.0.1:${HTTP_PORT:-80}` so it is not exposed on the host's
public interfaces.

The web service passes Cloudflare's `CF-Connecting-IP` to the app when present. The app uses that IP for login
audit records, lockouts, and automatic Cloudflare blocks; otherwise it falls back to the direct
loopback/tunnel client.

Set the tunnel token in `.env`:

```env
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

Then start the normal stack with `docker compose up -d --build`.

Background processors also run while the web app is up. Trip processing recalculates the current
local day on a short interval and finalizes completed local days. After trip processing updates its
checkpoint, processed OwnTracks location/event rows older than
`OWNTRACKS_LOCATION_RETENTION_DAYS` are purged automatically, with an enforced minimum retention of
90 days. Work trips, odometer fields, waypoints, reports, gas price records, monthly OwnTracks
summary rollups, backups, and other derived app data are not removed by this purge. The app
container also runs the daily gas snapshot scheduler when `GAS_SNAPSHOT_ENABLED=true`.

Useful Docker environment options:

```env
COMPOSE_PROFILES=local-postgres
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
LOCAL_TIMEZONE=America/Detroit
DATABASE_URL=postgresql+psycopg://triptracker:change-postgres-password@postgres:5432/trip_tracker
POSTGRES_DATA_VOLUME=trip-tracker-postgres-data
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_POOL_TIMEOUT_SECONDS=30
DATABASE_POOL_RECYCLE_SECONDS=1800
DATABASE_CONNECT_TIMEOUT_SECONDS=10
DB_WAIT_TIMEOUT_SECONDS=60
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
OWNTRACKS_PURGE_ENABLED=true
OWNTRACKS_LOCATION_RETENTION_DAYS=90
OWNTRACKS_WAYPOINT_DWELL_MINUTES=5
OWNTRACKS_TRAVEL_DISTANCE_M=50.0
OWNTRACKS_ENCRYPTION_KEY=change-owntracks-encryption-key
WEB_API_KEY=change-web-api-key
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
PASSKEY_RP_NAME=Trip Tracker
PASSKEY_RP_ID=
PASSKEY_ORIGIN=
CLOUDFLARE_IP_BLOCKING_ENABLED=false
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ZONE_ID=
CLOUDFLARE_IP_BLOCK_ALLOWLIST=
CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS=5
PUSHOVER_ENABLED=false
PUSHOVER_TOKEN=
PUSHOVER_USER=
PUSHOVER_APP_KEY=
PUSHOVER_USER_KEY=
PUSHOVER_DEVICE=
PUSHOVER_PRIORITY=0
APP_HEALTH_MONITOR_INTERVAL_SECONDS=60
APP_HEALTH_REMINDER_INTERVAL_SECONDS=3600
APP_HEALTH_DB_LATENCY_WARNING_MS=500
APP_HEALTH_DB_LATENCY_CRITICAL_MS=2000
APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS=15
APP_HEALTH_DISK_WARNING_FREE_MB=1000
APP_HEALTH_DISK_CRITICAL_FREE_MB=250
APP_HEALTH_STATE_PATH=/data/app-health-state.json
HTTP_PORT=80
APP_DATA_DIR=/data
HOST_DATA_DIR=/var/lib/trip-tracker
HOST_BACKUP_DIR=/var/lib/trip-tracker/backups
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/backups
AUTOMATIC_BACKUP_RETRY_SECONDS=60
MAX_BACKUP_RESTORE_BYTES=262144000
REPORT_DISPLAY_NAME=
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

Docker Compose passes `LOCAL_TIMEZONE` through as the container `TZ` value for the app stack.
Set both `WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` to enable login on rendered web pages while
the public web service exposes only the OwnTracks ingestion endpoints under `/api/`; production Docker
requires both values, `WEB_API_KEY`, `OWNTRACKS_ENCRYPTION_KEY`, OwnTracks Basic Auth credentials,
and a non-default `SECRET_KEY`. `WEB_LOGIN_MAX_ATTEMPTS` and
`WEB_LOGIN_LOCKOUT_SECONDS` control the temporary lockout for repeated failed attempts.
Passkeys are optional and are configured from Diagnostics after username/password login.
`PASSKEY_RP_NAME` controls the device prompt name. `PASSKEY_RP_ID` and `PASSKEY_ORIGIN` can be left
blank for normal same-host use, or set to the public HTTPS host and origin when a custom reverse
proxy does not forward the browser origin correctly.
When `CLOUDFLARE_IP_BLOCKING_ENABLED=true`, Diagnostics can create and remove app-managed
Cloudflare zone IP Access Rule blocks using `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID`.
`CLOUDFLARE_API_TOKEN` must be a Cloudflare API token with `Account Firewall Access Rules Write`
access for the configured zone; do not use `CLOUDFLARED_TUNNEL_TOKEN` or a Global API Key in that
field.
The app auto-blocks an IP after `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS` consecutive failed
web-login attempts, and a successful login from that IP resets the local consecutive-failure count.
The Cloudflare blocked-IP card also accepts a manual valid IP address plus a required reason, shows
manual or automatic source pills with the block reason in the app-managed block list, and removes
both the Cloudflare rule and local list row when you remove the block.
Set `CLOUDFLARE_IP_BLOCK_ALLOWLIST` to comma-separated trusted IPs or CIDRs that should never be
blocked by the app.
Set `PUSHOVER_ENABLED=true`, `PUSHOVER_TOKEN` to the Pushover app API token, and `PUSHOVER_USER`
to the Pushover user/group key to receive app-health notifications. `PUSHOVER_APP_KEY` and
`PUSHOVER_USER_KEY` are accepted aliases. The monitor watches PostgreSQL availability and
latency, free disk space, active web-login lockouts, and app-managed Cloudflare blocks. High
database latency must remain above a configured threshold for
`APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS` before Pushover sends it. Disk alerts use the adjustable
free-space amounts rather than a used percentage. The monitor sends a degraded or unavailable
notification when the monitored issue set changes, repeats an unchanged unhealthy state every
`APP_HEALTH_REMINDER_INTERVAL_SECONDS` (one hour by default), and sends one restored notification
when all monitored checks are healthy again.
The app writes all runtime, request, worker, trip-calculation, and debug logging to container
stdout/stderr. Use `docker compose logs -f ttapp` for Compose or
`docker service logs -f <stack>_ttapp` for Swarm. Successful and failed login audits are stored in
PostgreSQL and shown separately in Diagnostics; no audit or application log file is created.
Automatic backups default to `/data/backups`, backed by the dedicated `HOST_BACKUP_DIR` bind
mount, and are listed/restorable from Diagnostics after web login. Long automatic-backup filenames
are truncated in the Diagnostics table but remain visible on hover and available to download.
Backups created by the app startup pass are labeled as startup backups.
Diagnostics marks travel when recent OwnTracks movement outside saved waypoints covers at least
`OWNTRACKS_TRAVEL_DISTANCE_M` meters.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`; error lines are always included at every level.

## Gas Price Snapshot Scheduler

In Docker, the app container runs the gas price snapshot scheduler instead of a separate
`gas-snapshot` sidecar container. It uses the same code as the manual command:

```bash
trip-tracker gas-snapshot
```

By default Docker runs one snapshot on app startup and then every 24 hours. Configure it with:

```env
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

Set `GAS_SNAPSHOT_ENABLED=false` to disable the in-app scheduler. The manual command remains
available, so a host systemd timer can run `docker compose exec -T ttapp trip-tracker gas-snapshot`
on a schedule without cron if you prefer host-managed timing. The Docker image itself does not run
systemd inside the container.
Set `REPORT_DISPLAY_NAME` when downloaded PDF reports should identify who submitted the report; the
name appears under the report title as `Submitted by:`.
The PDF title uses `Mileage & Expense Report` plus the selected month and year. The Work Trips
page can add up to five extra expense lines per report month, and those lines appear after trip
rows in the PDF with date, reason, and price. The PDF summary highlights the final total
reimbursement dollar amount with a yellow background.

## Workflow

1. Create work waypoints in OwnTracks and publish/export them to the server.
2. Review or export saved waypoints from the `Waypoints` page.
3. Configure OwnTracks to send waypoint transition events and normal location updates.
4. Let the app automatically create work trips from incoming OwnTracks transitions.
5. Review `Work Trips`, choose the needed month/year with the month picker, edit row waypoint
   dropdowns or miles if needed, and add manual work trips with the local-today date default.
   Existing row dates and odometers are read-only. The summary cards show the selected month's work
   trip plus non-work trip miles, work-trip-only miles, OwnTracks events, work trip count,
   reimbursement, and monthly average gas price.
6. Add optional extra report expenses for the selected month, up to five lines.
7. Add or fetch a monthly gas price for that report month.
8. Download the portrait monthly PDF report from the `Work Trips` page.

## Project Commands

```bash
ruff check .
pytest
bash -n scripts/*.sh
CLOUDFLARED_TUNNEL_TOKEN=dummy-token docker compose --env-file .env.docker.example config
docker compose run --rm ttapp alembic revision --autogenerate -m "message"
docker compose run --rm ttapp alembic upgrade head
```
