# Changelog

## 1.4.4 - 07.26.2026

### Added
- Added a red Diagnostics Emergency Rebuild action that creates a full safety backup, treats the
  entered reading as the current master odometer, and rebuilds all displayed trip odometers without
  changing any stored trip distances.
- Added best-effort emergency gap recovery from retained OwnTracks history, with automatic removal
  of gaps at or above 200 miles and largest-first removal of smaller gaps only when needed to keep
  the rebuilt odometer sequence valid.
- Added trip-generation coverage for journeys that start before midnight and finish after midnight.

### Changed
- Changed manual odometer saves to require the exact `Home` waypoint in both the Diagnostics UI and
  server route; the normal Save button is disabled away from Home while Emergency Rebuild remains
  available.
- Changed a trip-distance edit to recalculate only the edited trip and later trips in that trip's
  start month, leaving earlier trips and every other month unchanged.
- Changed cross-midnight trip processing to assign the trip to its local start day and include
  OwnTracks path events through the actual arrival event after midnight.
- Bumped the Mileage Logger package version to 1.4.4.

### Fixed
- Fixed late-month trip-distance corrections being able to replay corrupted gaps across the whole
  month and produce implausibly large displayed odometers.
- Fixed Home-based manual alignment failing when oversized historical gaps could not fit below the
  entered current odometer.

## 1.4.3 - 07.17.2026

### Added
- Added regression coverage for the compact authenticated page layout and the new footer placement
  of Dashboard app time and the Waypoints export action.

### Changed
- Bumped the Mileage Logger package version to 1.4.3.
- Removed the top-level title, description, and separator from Dashboard, Waypoints, and
  Diagnostics while preserving their functional card and table headings.
- Changed Work Trips to place its title and selected-month description beside the month and PDF
  controls in one compact, bottom-aligned desktop row directly above the page separator.
- Moved the OwnTracks waypoint export action to the bottom of Waypoints and the app-local time to
  the bottom of Dashboard.

### Fixed
- Fixed unnecessary introductory header space pushing the primary authenticated page content
  farther down the screen.

## 1.4.2 - 07.15.2026

### Added
- Added regression coverage for Home-based manual odometer alignment, preserved between-trip
  gaps, and strict master odometer ownership.
- Added a three-color key below Monthly Work Trips that explains automatic, edited, and manual row
  shading.

### Changed
- Bumped the Mileage Logger package version to 1.4.2.
- Changed manual odometer entries made while OwnTracks reports the vehicle inside the `Home`
  waypoint to align all displayed trip odometers so the latest trip end matches the entered
  reading while preserving each trip's mileage and every positive between-trip gap.
- Changed master rolling odometer ownership so only OwnTracks distance processing and an explicit
  manual odometer entry can update it.
- Changed edited Work Trips from a compact Edited pill to a full-row purple tint, distinct from
  blue automatic rows and gold manual rows.

### Fixed
- Fixed Home-based trip odometer recalculation collapsing non-trip driving gaps into continuous
  trip-to-trip odometer chains.
- Fixed trip creation, editing, resequencing, and missing-odometer repair being able to roll the
  master odometer forward from a trip row.

## 1.4.1 - 07.13.2026

### Added
- Added `HOST_BACKUP_DIR` as a dedicated Docker Compose and Swarm bind mount for automatic
  backups, separate from other persistent application state.
- Added adjustable `AUTOMATIC_BACKUP_RETRY_SECONDS`,
  `APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS`, `APP_HEALTH_DISK_WARNING_FREE_MB`, and
  `APP_HEALTH_DISK_CRITICAL_FREE_MB` Docker settings.

### Changed
- Changed automatic backup storage to the dedicated `/data/backups` bind mount so Swarm hosts can
  use `mileage-logger/backups` without retaining the former `logs/backups` layout.
- Changed Pushover database-latency alerts to require sustained high latency for 15 seconds by
  default, while keeping the Diagnostics latency reading immediate.
- Changed disk-space health alarms from used-percentage thresholds to free-space thresholds of
  1,000 MiB for warning and 250 MiB for critical by default.
- Bumped the Mileage Logger package version to 1.4.1.

### Fixed
- Fixed automatic backups waiting the full six-hour schedule after shared storage becomes
  unavailable, including stale file handle errors; failed backup passes now pause and retry every
  60 seconds by default until a backup succeeds.
- Fixed an older automatic trip with the same recorded day, route, distance, and odometer interval
  but slightly different OwnTracks event times repeatedly violating PostgreSQL uniqueness, rolling
  back the processing checkpoint, and blocking all later trips from being generated.

## 1.4.0 - 07.12.2026

### Added
- Added the dedicated `mileage-internal` Swarm overlay network for the application, reverse proxy,
  Cloudflare Tunnel connector, and optional bundled PostgreSQL service.
- Added a GitHub Actions workflow that publishes versioned, `latest`, and immutable commit-SHA app
  and nginx container images to GitHub Container Registry for multi-node Swarm deployments.
- Added retry-safe OwnTracks HTTP outage responses with `503 Service Unavailable`,
  `Retry-After: 30`, and no-store caching so mobile devices retain messages until PostgreSQL
  returns.
- Added exact HTTP retry detection so a resent OwnTracks event does not create a duplicate raw
  location row.

### Changed
- Renamed the Docker Compose and Swarm application services from `app` and `nginx` to the
  app-specific `mlapp` and `mlnginx` names, including internal DNS, Cloudflare Tunnel origin, logs,
  maintenance commands, tests, and documentation. Existing deployment variable names are unchanged.
- Bumped the Mileage Logger package version to 1.4.0 for the HTTP-only OwnTracks ingestion and
  Swarm deployment changes.
- Changed OwnTracks ingestion to HTTP-only direct PostgreSQL storage, returning `200 []` only
  after migrations are ready and the payload commit succeeds.
- Changed database-outage mode to rely on the OwnTracks mobile app's queue while preserving the
  browser service-unavailable page, health alerts, and paused database-writing schedulers.
- Changed Docker Compose and Swarm storage to require only the app data mount for backups and
  health state.
- Changed the Swarm app task to use configurable `APP_UID` and `APP_GID` values so shared-storage
  ownership can be matched without running the application as root.
- Changed the Swarm Cloudflare Tunnel connector to two replicas placed one per node, with a
  five-second restart delay and start-first rolling updates for faster connector failover.
- Changed upgrades to require the former server-side OwnTracks queues to be fully drained before
  deploying this HTTP-only ingestion path.

### Fixed
- Removed the MQTT dependency, worker, configuration, and documentation because MQTT ingestion is
  no longer supported.
- Removed the server-side SQLite OwnTracks buffers, replay worker, mounts, health signals, and
  Diagnostics queue indicators.
- Fixed the Swarm `WEB_API_KEY` interpolation message so the legacy stack parser preserves the
  configured secret instead of rendering a predictable literal value.

## 1.3.4 - 07.11.2026

### Added
- Added a `This is a public device` login option with a 15-minute inactivity timeout, browser-data
  cleanup on timeout/logout, and immediate Device Sign-In disabling while selected.
- Added PostgreSQL-backed successful and failed web-login audit records for Diagnostics.
- Added database-level partial unique indexes for automatic source-event signatures and identical
  day/route/distance/odometer intervals.
- Added regression coverage for a short false waypoint visit where OwnTracks sends an `enter` and
  `leave` before the configured dwell time.
- Added a compact Edited indicator for automatic Work Trips rows whose saved mileage was corrected.
- Added progressive pagination behavior for paginated Waypoints and Diagnostics lists so page
  buttons can update only the active list without a full browser navigation.

### Changed
- Changed the public-device login explanation from persistent text to an accessible tooltip shown
  when the checkbox row is hovered or keyboard-focused.
- Changed all application, request, worker, trip-calculation, and debug logging to console-only
  output for Docker Compose and Docker Swarm log collection.
- Changed persistent runtime storage from log-oriented `HOST_LOG_DIR`/`LOG_DIR` paths to
  `HOST_DATA_DIR`/`APP_DATA_DIR`, with automatic backups under `/data/backups`.
- Changed Diagnostics login history and its JSON Lines export to read from PostgreSQL instead of a
  host log file, and removed the App Log panel, refresh button, download button, and file endpoint.
- Bumped the Mileage Logger package version to 1.3.4.
- Changed Work Trips row source handling so only trips created from the Add Work Trip form use the
  manual yellow row tint.
- Changed paginated Waypoints and Diagnostics list controls to keep the current scroll position
  while loading First, Previous, Next, and Last pages.
- Changed trip generation so a waypoint `leave` cannot become a trip origin when that waypoint's
  matching arrival was rejected for leaving before the dwell deadline.

### Fixed
- Fixed exact automatic trips being recorded more than once by keeping the oldest existing
  duplicate during migration and rejecting future duplicate event or recorded-value signatures in
  PostgreSQL.
- Fixed automatic Work Trips turning into manual-colored rows when only their distance was edited.
- Fixed short false waypoint visits, such as a nearby stop that OwnTracks briefly labels as inside
  a waypoint, from generating a return trip from that waypoint.
- Fixed paginated lists jumping back to the top of the page when switching pages.

## 1.3.3 - 07.08.2026

### Added
- Added regression coverage for pending waypoint arrivals, delayed same-waypoint departures.
- Added forward-only master odometer sync coverage for cases where the latest trip end is ahead of
  the current rolling odometer.
- Added the current app version below the Mileage Logger title in the authenticated desktop top
  navigation bar.

### Changed
- Bumped the Mileage Logger package version to 1.3.3.
- Changed the Work Trips table to shade automatic trip rows blue, keep manual trip rows yellow,
  and shade deleted-trip records by manual or automatic source.
- Changed master odometer handling to roll the checkpoint forward to the latest trip end odometer
  only when that trip end is higher than the current master odometer, without ever rolling it back.
- Changed waypoint dwell confirmation to use all available OwnTracks state evidence after an
  inside-radius arrival, including later same-waypoint departures, later next-waypoint arrivals,
  and the next processing pass after the dwell timer when no earlier event contradicts the visit.
- Changed OwnTracks-named arrivals outside the saved waypoint radius to wait for later
  same-waypoint state evidence before confirming a trip destination.

### Fixed
- Fixed automatic trip detection so a same-waypoint `leave` after the dwell window confirms the
  prior waypoint arrival instead of rejecting it.
- Fixed Home-to-work trips being lost when a named work waypoint arrival was outside the saved
  waypoint radius but was later followed by a same-waypoint leave after the dwell window.
- Fixed invalid username/password login attempts so the browser stays on the sign-in page and
  shows a top status-line error instead of being replaced by the public 401 error page.

## 1.3.2 - 07.07.2026

- Bumped the Mileage Logger package version to 1.3.2.
- Added a green/yellow/red database latency status indicator to the Diagnostics System Status card.
- Kept the browser favicon on the new Mileage Logger logo and set icon responses to no-store
  caching so browsers pick up refreshed assets.
- Changed Apple touch and installable web-app icons to use launcher-safe padding so mobile home
  screen masks do not crop the logo, and versioned manifest icon URLs so new installs fetch the
  refreshed assets.
- Reduced the installable mobile web-app icon artwork size again so the logo fits more comfortably
  inside home-screen icon masks.
- Reduced the installable icon artwork one final small step to avoid slight gauge clipping at the
  top of mobile home-screen masks.
- Shrunk the installable icon artwork more noticeably and nudged it downward so the gauge top stays
  inside mobile home-screen masks.
- Moved the installable home-screen icon artwork slightly upward while keeping the reduced safe-area
  sizing.
- Changed the Dashboard Work Trips card to show today, current Monday-Sunday week, and current
  month work-trip counts inside the same card.
- Changed the mobile Dashboard Work Trips card to keep the today, week, and month counts in one
  row when the phone width allows it.
- Changed the Dashboard Work Trips card labels to Today, Week, and Month, aligned the values on one
  row, and made the mobile values larger.
- Fixed automatic waypoint trip detection so destination dwell confirmation requires later stored
  coordinates inside the saved waypoint radius, preventing loose OwnTracks region labels or elapsed
  processor time alone from creating drive-by trips.

## 1.3.1 - 07.04.2026

- Bumped the Mileage Logger package version to 1.3.1.
- Added manual extra expense lines to monthly PDF reports from the Work Trips page, with a hard
  five-expense cap per report month.
- Added extra expense rows to the monthly PDF below trip rows, plus an extra expense total row that
  is included in the final total reimbursement amount.
- Kept individual PDF extra expense rows unhighlighted so only the final total reimbursement value
  uses the yellow highlight.
- Changed the monthly PDF title to `Mileage & Expense Report` while keeping the selected month and
  year in the title.
- Added database latency, size, total-record, pool, and timeout details to the Diagnostics System
  Status card, and simplified the primary and backup buffer rows to status-only indicators.
- Added optional Pushover app-health notifications for degraded or unavailable app state, plus a
  restored notification when monitored checks are healthy again.
- Added a yellow/red Diagnostics app-health banner for database, buffer, disk-space, web-login
  lockout, and app-managed Cloudflare block issues.
- Replaced the web favicon, header brand icon, Apple touch icon, and installable mobile web-app
  icons with the new Mileage Logger logo.
- Added transparent logo source variants and changed the authenticated header to use a transparent
  brand logo while keeping the original square logo for installed mobile app icons.
- Removed app logo, app-name, manifest, favicon, and Apple touch icon metadata from the login page.
- Moved Add Work Trip below the Monthly Work Trips list and above Extra Report Expenses.
- Fixed database-outage recovery navigation to retry the app base URL instead of `/login`, avoiding
  a 401 error page when service returns.
- Changed changelog headings to use unbracketed version numbers with `MM.DD.YYYY` release dates.

## 1.3.0 - 07.02.2026

- Bumped the Mileage Logger package version to 1.3.0.
- Fixed changelog headings to use consistent version labels and `MM.DD.YYYY` release dates.
- Added a saved color palette sample sheet for choosing a future app theme.
- Added lowest, current, monthly average, and highest gas price readings to the Diagnostics Data
  card.
- Changed Dashboard, Work Trips, and Diagnostics summary card numbers to use comma thousands
  separators for large displayed values.
- Removed the local-development `.env.example` sample and local app-run instructions so the app is
  documented and defaulted as Docker-only.
- Changed the PDF report title date to show the selected month name and year.
- Added central PostgreSQL readiness by making app startup wait on the configured `DATABASE_URL`
  and exposing configurable PostgreSQL pool and timeout settings for network database deployments.
- Added a default-on `local-postgres` Compose profile so installs can keep the bundled PostgreSQL
  container or set `COMPOSE_PROFILES=` and use only a remote `DATABASE_URL`.
- Fixed malformed `DATABASE_URL` startup handling so the app can still enter OwnTracks buffer limp
  mode instead of crashing during import, and documented URL-encoding database passwords.
- Fixed bare `postgresql://` database URLs so they use the installed psycopg v3 driver instead of
  trying to import unavailable psycopg2.
- Added Docker Swarm stack files for remote PostgreSQL and optional bundled PostgreSQL deployments.
- Added an app healthcheck start period so Docker Swarm does not restart the app while startup is
  waiting for PostgreSQL before entering OwnTracks buffer limp mode.
- Fixed database-outage web rendering so Dashboard and Work Trips content fetches receive only the
  limp-mode warning panel instead of nesting a second top bar, and disabled non-Home navigation
  while the limp-mode page is active.
- Changed the database-outage warning page to use a larger `Service Temporarily Unavailable`
  heading, refreshed explanatory text, a 30-second login-page retry, and side-by-side primary and
  backup buffer cards.
- Removed shared app chrome, navigation, connection details, OwnTracks-specific labels, intake
  status, replay errors, and bottom explanatory text from the database-outage warning page.
- Changed the database-outage page queue status labels to generic primary/backup queues and showed
  the oldest queued payload as an elapsed age instead of a timestamp.
- Removed the database status card from the database-outage warning page and aligned the queued
  payload count with the oldest received payload age on the same row.
- Expanded `.env.docker.example` comments so Docker and Portainer configuration variables are
  easier to understand before deployment.
- Added a Diagnostics System Status card showing PostgreSQL availability and whether the
  configured PostgreSQL host is remote, plus primary and backup OwnTracks buffer availability with
  red/green status indicators.
- Added default-on OwnTracks limp-mode buffering so the app can keep accepting OwnTracks HTTP and
  MQTT payloads into a persistent local queue when PostgreSQL is unreachable, then replay them in
  receive order after the database returns.
- Added a local Docker named-volume fallback OwnTracks buffer for cases where the primary
  host/NFS-backed buffer path is unavailable, with replay held until both queues can be drained in
  receive order unless primary failure was observed before the database outage.
- Changed automatic full-data backups from hourly to every 6 hours and reduced recent automatic
  backup retention to one day while keeping one daily backup for each of the prior 2 days.
- Highlighted the PDF report's final total reimbursement value with a yellow background.
- Tightened the PDF report header spacing so the title starts directly below the top margin, the
  submitted-by line sits closer to the title, and the trip table starts closer to the header.

## 1.2.4 - 07.02.2026

- Bumped the Mileage Logger package version to 1.2.4.
- Removed the ID column from the Diagnostics Recent OwnTracks Entries table.
- Fixed public web 404 handling so unknown browser page URLs proxied through the bundled nginx web
  service use the custom error page instead of FastAPI's JSON `{"detail":"Not Found"}` response.
- Fixed generated work trip odometer assignment so automatic trips use stamped OwnTracks rolling
  odometer readings or the master rolling checkpoint instead of carrying forward the previous work
  trip end odometer.
- Fixed generated and already-recorded work trips with blank odometers so retained OwnTracks path
  rows can derive trip start and stop odometers from the master rolling checkpoint.
- Fixed Dashboard OwnTracks Events and Work Trips count cards so they reset at the current
  America/Detroit month boundary instead of showing all-time counts.
- Changed the legacy previous-month reset path to retain historical month data instead of deleting
  prior-month OwnTracks and gas snapshot rows at rollover.
- Added monthly OwnTracks summary rollups so selected-month web totals and event counts remain
  stable after old raw OwnTracks location/event rows are purged.
- Added optional `REPORT_DISPLAY_NAME` Docker configuration so downloaded PDF reports can identify
  the report submitter under the title.
- Changed downloaded PDF reports from landscape to portrait layout.
- Tightened downloaded PDF report margins and widened the From and To columns.
- Changed OwnTracks raw location/event retention to a minimum of 90 days and kept automatic
  cleanup limited to raw OwnTracks rows only.
- Changed trip deletion and trip odometer resequencing so trip rows no longer update the master
  rolling odometer checkpoint; only OwnTracks location processing and manual odometer entries move
  that checkpoint.

## 1.2.3 - 06.30.2026

- Bumped the Mileage Logger package version to 1.2.3.
- Changed visible Trips labels to Work Trips and visible non-trip labels to Non-Work Trips.
- Changed the Diagnostics Recent OwnTracks Entries table timestamp header from Captured to
  Original while keeping the received-delay calculation unchanged.
- Changed the Diagnostics OwnTracks State Changes table so Received Delay appears before state
  details and Duration appears later in the row.
- Added OwnTracks HTTP payload encryption support using `OWNTRACKS_ENCRYPTION_KEY`; OwnTracks
  ingestion now requires matching HTTP Basic Auth and decryptable encrypted payloads.
- Added `WEB_API_KEY` bearer-token protection for non-OwnTracks `/api/*` routes while keeping
  `/api/health` available for internal health checks.
- Added matching nginx error pages for common 4xx and 5xx responses with general end-user
  explanations and a link that returns authenticated users home instead of to login.
- Changed web error pages so the HTTP error label is the largest text, the plain-language title is
  secondary, and public-facing page copy uses generic web service wording.
- Reduced the web error page HTTP error label size so it stays only slightly larger than the
  plain-language title.
- Changed Docker nginx publishing to loopback-only on `127.0.0.1:${HTTP_PORT:-80}` for Cloudflare
  Tunnel use.
- Removed the trusted proxy CIDR configuration path; login audit records, lockouts, and Cloudflare
  auto-blocks now use Cloudflare's provided client IP when present and otherwise the direct client.
- Changed shared top navigation buttons to one blue raised style across desktop and mobile.
- Centered the shared top navigation button group within the full-width desktop top bar.
- Added raised, hover-brightened, pressed-in interaction styling to app buttons and button-style
  links while preserving non-navigation button colors.
- Changed Dashboard top statistic and distance cards to use the same compact card sizing as the
  Work Trips selected-month cards on full-width layouts while preserving mobile card stacking.

## 1.2.2 - 06.29.2026

- Bumped the Mileage Logger package version to 1.2.2.
- Changed the Dashboard home card order so Location State is the first card shown.
- Removed the Distance column from the Diagnostics OwnTracks State Changes table.
- Added Duration, Source, Received Delay, and Rolling Odometer columns to the Diagnostics
  OwnTracks State Changes table.
- Changed the Diagnostics Successful Login Attempts table to replace the Account column with a
  Password or Passkey method pill.
- Changed the top Diagnostics cards to render as one grouped three-column desktop grid ordered by
  overview, current state, actions, and storage: Application, Data, Latest Records, OwnTracks State,
  Manual Odometer, EIA API, Configure Passkey, and Hard Drive Space.
- Removed the separate `WEB_CHANGELOG.md` file because Mileage Logger is a single-user app and
  release notes now live only in `CHANGELOG.md`.
- Changed the Diagnostics Recent OwnTracks Entries table to show received delay and readable event
  labels, and removed the Battery and raw Topic columns.
- Changed authenticated navigation so desktop and mobile use matching color-coded buttons, with
  desktop showing icons beside labels, mobile using compact icons, and unauthenticated login views
  keeping no top nav.

## 1.2.1 - 06.27.2026

- Bumped the Mileage Logger package version to 1.2.1.
- Added a lightweight Dashboard loading shell so direct homepage loads show a loading message while
  the calculated Dashboard content is fetched from an authenticated content route.
- Added a lightweight Trips loading shell so selected-month cards and trip rows are fetched from
  `/trips/content` after the initial Trips page opens.
- Changed Trips month navigation to a single month/year picker that defaults to the current local
  month, auto-loads selected months, and displays the selected month as `Showing June 2026
  (06/2026)` style text.
- Added compact selected-month summary cards to Trips above Add Trip for trip plus non-trip miles,
  trip-only miles, OwnTracks events, trip count, reimbursement, and monthly average gas.
- Added WebAuthn passkey login with a Device Sign-In button on the login page and a Configure
  Passkey card on Diagnostics for creating, listing, and removing the single configured user's
  passkeys.
- Changed the login page to place Device Sign-In below the normal password Continue button.
- Added the `passkey_credentials` database table plus optional `PASSKEY_RP_NAME`,
  `PASSKEY_RP_ID`, and `PASSKEY_ORIGIN` settings for public-origin WebAuthn validation.
- Added successful web-login audit records and a paginated Successful Login Attempts table above
  the Failed Login Attempts table on Diagnostics.
- Changed automatic backups created by the app startup pass to use a startup-marked filename and
  show a Startup label in the retained automatic-backup table.
- Changed Waypoints and Diagnostics mobile pagination so First, Previous, Next, and Last stay in
  one full-width row with the page count shown as plain text below.
- Changed the shared top-bar brand icon and Mileage Logger text to display-only content instead of
  a clickable home link.
- Added a manual valid-IP Cloudflare block form to Diagnostics, requiring a reason and showing
  Auto/Manual source pills with each reason in the app-managed blocked-IP list with per-row removal
  from Cloudflare and the local list.
- Changed Cloudflare authentication failures to explain that `CLOUDFLARE_API_TOKEN` must be a
  Cloudflare API token with `Account Firewall Access Rules Write` access, not the tunnel token or a
  Global API Key.
- Fixed web-login security startup checks so production fails closed without configured login
  credentials and a changed `SECRET_KEY`, and enabling web login in any environment rejects the
  default session secret.
- Fixed login lockout and Cloudflare auto-block identity handling so bundled nginx selects one
  Cloudflare-derived client IP and overwrites spoofable forwarded client IP headers before proxying.
- Fixed Diagnostics login rows and Cloudflare block buttons to correct stale proxy/container
  `client_ip` values from trusted forwarded headers for both successful and failed web-login
  attempts.
- Fixed bundled nginx and Diagnostics login audit handling so successful and failed web-login rows
  use the Cloudflare-derived client IP when Cloudflare Tunnel supplies `CF-Connecting-IP`, instead
  of losing that value when the tunnel origin is not loopback.
- Changed bundled nginx to forward the public HTTPS scheme from loopback `cloudflared` traffic so
  passkey origin checks can match the browser's Cloudflare Tunnel origin.
- Fixed monthly PDF generation so trip and waypoint names are escaped before ReportLab parses
  table cell text.
- Changed CI Docker Compose validation to use `.env.docker.example` through `--env-file` with a
  dummy tunnel token instead of leaving a production `.env` file behind before tests.

## 1.2.0 - 06.24.2026

- Changed desktop navigation links to use the same boxed button treatment as Logout.
- Changed the mobile web-app shell so the top navigation buttons span the full width, the mobile
  close/title controls stay removed, fixed bottom navigation stays removed, the viewport no longer
  opts into phone edge-to-edge drawing, and the manifest includes a browser fallback for phone
  system navigation.
- Changed install metadata responses to use no-store caching so phones pick up updated manifest
  and service-worker shell settings promptly.
- Added the app version to the Diagnostics Application card.
- Bumped the Mileage Logger package version to 1.2.0.
- Fixed generated trip odometer starts to prefer the newest stored rolling checkpoint before the
  trip over older prior-trip odometers, then calculate the end odometer from that start plus the
  generated trip distance.

## 1.1.4 - 06.23.2026

- Changed Diagnostics hard drive space grouping to combine configured runtime paths only when exact
  used bytes and total bytes match.
- Bumped the Mileage Logger package version to 1.1.4 for dev-branch testing before release.
- Changed the Dashboard summary cards to remove the Waypoints card, move Trips into that slot, and
  show the current-month reimbursement total using the same mileage, gas price, and MPG formula as
  the downloadable PDF report summary, with one-decimal reimbursement gallons shown under the
  price.
- Tightened Diagnostics list cards so recent OwnTracks entries, OwnTracks state changes, failed
  login attempts, and app-managed Cloudflare blocked IPs show 10 rows per page, shortened the app
  log window, and made automatic-backup rows slimmer with truncated filenames and accessible
  restore confirmation inputs.
- Removed the separate Docker `gas-snapshot` service and moved recurring gas price snapshots into
  the app container background scheduler while keeping the manual `mileage-logger gas-snapshot`
  command available.
- Moved the Diagnostics manual full-backup description and `Download Full Backup` button down to
  the lower upload-restore area of the Full Data Backup card.
- Removed the Failed Login Attempts card refresh and download action row from Diagnostics so the
  card fits its table content more tightly.
- Added a Docker host-port binding setting and changed bundled `cloudflared` to host networking so
  Cloudflare Tunnel can route to that bound host listener.
- Added Diagnostics controls to hide failed-login rows, manually block/unblock failed-login IPs at
  Cloudflare, list app-managed Cloudflare IP blocks, and automatically block an IP after the
  configured consecutive failed-login threshold.
- Added database size and total app-record count totals to the bottom of the Diagnostics hard drive
  space card.
- Changed the Trips page to show newest trip dates first while leaving the Dashboard recent trips
  unchanged.
- Added used-space display bars to the Diagnostics hard drive space card.
- Added a Diagnostics hard drive space card that combines configured runtime paths when exact used
  bytes and total bytes match, reducing duplicate same-drive rows.
- Added per-file download buttons for retained automatic backups on Diagnostics, using the same web
  login guard, filename validation, restore size limit, and no-store caching as other backup
  downloads.
- Changed manual trip creation to start from the current rolling OwnTracks odometer checkpoint when
  available, place new manual trips after existing trips on the selected local date, and preserve
  existing positive non-trip odometer gaps when resequencing later trips.
- Changed Trips page manual-entry and row-edit forms to use saved waypoint dropdowns for From/To,
  with manual trip dates defaulting to today's `LOCAL_TIMEZONE` date.
- Fixed Dashboard trip plus non-trip distance totals so the combined total is never lower than the
  trips-only total and the implied non-trip remainder is never negative after one-decimal rounding.
- Added hourly automatic full-data backups under `AUTOMATIC_BACKUP_DIR`, with Diagnostics listing
  retained files and supporting typed-confirmation restore from a selected automatic backup.
- Fixed Dashboard today and month trip plus non-trip totals so they are summed from OwnTracks
  coordinate path data instead of rolling odometer differences, preventing manual odometer resets
  from inflating driven-mile totals.
- Changed automatic trip generation so odometer deltas are never used as the trip distance source;
  transition-only trips fall back to waypoint distance while odometer fields remain display values.
- Moved the Diagnostics Full Data Backup card to the bottom of the page under the App Log.
- Changed the Diagnostics layout so Manual Odometer, EIA API, and OwnTracks State share one
  equal-width card row.
- Changed the Diagnostics manual odometer card to show the current odometer reading before saving
  a new manual checkpoint.
- Fixed manual trip creation so new manual trips save start/end odometers from the latest known
  odometer reading, and prior-date manual inserts resequence every later trip odometer
  cumulatively across month boundaries.
- Added a restore regression check proving full backup restore replaces changed same-row data
  instead of creating duplicate rows.
- Changed public nginx routing so only rendered web pages and OwnTracks ingestion endpoints are
  internet-facing; all other `/api/` routes and generated FastAPI docs are blocked at nginx while
  internal container health checks still use `/api/health`.
- Documented that PostgreSQL data persists in the named Docker `postgres_data` volume across normal
  rebuilds, and warned against `down -v`, volume pruning, or stack-name changes without backup.
- Added Diagnostics full app data backup and restore controls. Backups download as sensitive
  `.json.gz` files containing all app database tables plus OwnTracks waypoint export, while restore
  requires web login, a validated file, and typed confirmation before replacing current app rows.
- Fixed Docker startup by removing the individual failed-login log file bind mount; failed-login
  audit records now use the shared host log directory with an optional `/var/log/...` symlink.
- Added structured failed-login audit logging, including client IP details, submitted username,
  password length, user agent, lockout state, and timestamps without storing raw passwords;
  Diagnostics now shows and downloads those entries.
- Changed Docker logging so app and worker logs bind to a host log directory and the container
  prepares mounted log paths before dropping to the non-root app user.
- Changed Docker environment generation so `WEB_LOGIN_PASSWORD` is generated instead of leaving
  the template placeholder in new `.env` files.
- Changed Trips page row editing so existing trip dates render as read-only text and cannot be
  changed by the row update form.
- Added a mobile-only top-bar close button that calls the browser close action for installed full-screen
  mobile web-app sessions.
- Added installable mobile web-app metadata, home-screen icons, and full-screen mobile shell styling
  so Mileage Logger opens like a phone app when saved to the home screen.
- Changed automatic trip generation so same-waypoint trips under 1.0 mile are
  suppressed as invalid non-trips. Existing automatic rows matching that rule
  are removed with an exact deleted-trip suppression record so the same
  OwnTracks transition pair does not recreate them.
- Fixed Dashboard Today distance cards so the trip and non-trip totals stay on the
  `LOCAL_TIMEZONE` day until local midnight instead of rolling over with UTC.
- Fixed automatic trip processing so a waypoint arrival can create the trip after the dwell timer
  expires even when OwnTracks sends no follow-up location rows while the phone remains there.
- Replaced the Dashboard Vehicle MPG card with a current OwnTracks location state card showing
  inside-waypoint, driving, stationary, or no-data status.
- Changed PDF trip table headers to spell out Start Odometer and End Odometer instead of
  abbreviating odometer.
- Added Dashboard distance cards for today's total driven miles, today's trip miles, this month's
  total driven miles, and this month's trip miles.
- Changed trip deletion records to be documented and displayed as exact deleted-trip records, not
  route-pattern rules, so future trips with the same route are still generated normally.
- Changed trip deletion to preserve the rolling odometer checkpoint from the deleted trip when that
  trip has the most recent odometer reading.
- Added a stored OwnTracks odometer timeline so every processed location row records the rolling
  odometer value used by later trip generation.
- Changed automatic trip processing to advance the rolling OwnTracks odometer before generating
  trips, allowing generated trip start/end odometer displays to follow OwnTracks-derived movement
  without making odometer deltas a distance source.
- Changed automatic trip generation so waypoint arrivals require a five-minute OwnTracks dwell
  confirmation before a trip is created, preventing drive-through waypoint trips.
- Added rolling checkpoint odometer updates from OwnTracks path distance outside generated trips,
  while Diagnostics manual odometer readings reset the checkpoint to a new rolling value.
- Removed external vehicle odometer integration and now derives odometer movement from OwnTracks
  path distance plus optional manual checkpoint corrections.
- Changed trip distances and odometer values to store and display one decimal place.
- Removed speed-based Diagnostics movement handling in favor of distance-based travel detection.
- Added Waypoints page delete buttons that remove stale app waypoints while preserving historical
  trip details.
- Changed Trips page row editing so odometers are read-only, while waypoint and distance edits
  automatically preserve the trip route metadata and resequence that month's trip odometers when
  mileage changes.
- Added a simple session-based web login for rendered pages, configured by Docker environment
  variables while leaving `/api/` routes outside the app-level web login.
- Removed visible app branding from the login page and added temporary failed-login lockouts.
- Added an editable deleted-trip records list on the Trips page so mistaken automatic-trip
  deletion records can be removed.
- Added Diagnostics current OwnTracks state detection for inside-waypoint and travel statuses,
  plus a state-change log limited to waypoint arrivals, waypoint departures, and travel detected.
- Added a Trips page manual-entry form so date, origin, destination, and distance can be entered
  manually.
- Added a Diagnostics page manual odometer form that updates the rolling checkpoint used for
  future OwnTracks-derived odometer estimates.
- Added automatic checkpoint-aware OwnTracks location retention so old processed raw location data
  is purged after the configured retention window without deleting trips or other app data.
- Changed generated trip mileage to prefer summed OwnTracks location path distance between
  waypoint leave/enter events before falling back to waypoint distance.
- Added a Trips page delete button and exact deleted-trip records so user-deleted automatic trips
  are not recreated from the same OwnTracks transition events.
- Fixed automatic trip generation so unchanged existing trips are not rewritten and counted as
  generated on every processing pass.
- Added `cloudflared` to the normal Docker Compose stack as a required Cloudflare Tunnel service
  configured by environment variables.
- Changed automatic trip processing to use a persistent OwnTracks checkpoint and append/update trips in place without deleting existing trip rows.
- Fixed checkpoint table startup recovery so automatic trip processing creates the missing table safely before querying it.
- Stopped ignoring Alembic migrations so database schema updates are included in normal commits and Docker builds.
- Added diagnostics page API test cards for EIA, plus last OwnTracks received age.
- Fixed app log download handling and added an app log refresh action.
- Redacted sensitive query values such as API keys from app log formatting, display, and download.
- Updated diagnostics log colors so DEBUG is green and INFO is white.
- Removed the OwnTracks Region ID column from the Waypoints page.
