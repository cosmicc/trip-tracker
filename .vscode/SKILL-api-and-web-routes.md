# Skill: Adding API Endpoints and Web Routes

**Purpose**: Guide AI agents through adding new API endpoints, web pages, authentication, and form handling in FastAPI and Jinja2.

## Overview

Trip Tracker uses:
- **API routes** (`trip_tracker/api/routes.py`) — JSON responses for OwnTracks integration
- **Web routes** (`trip_tracker/web/routes.py`) — Server-rendered HTML pages via Jinja2
- **Authentication** — Web login (session-based) + OwnTracks API authentication
- **FastAPI** — Modern async web framework with automatic OpenAPI docs

---

## API Routes Structure

### Location

[trip_tracker/api/routes.py](trip_tracker/api/routes.py)

### Basic Pattern

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from trip_tracker.database import get_db

router = APIRouter()

@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@router.post("/trips/{trip_id}")
def update_trip(
    trip_id: int,
    update: TripUpdate,  # Pydantic model from schemas.py
    db: Session = Depends(get_db),
) -> dict[str, str]:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    # ... update logic ...
    return {"status": "updated"}
```

### Key Features

- **Pydantic models** (`schemas.py`) for request/response validation
- **`Depends(get_db)`** to inject database session
- **Automatic documentation** at `/docs` (Swagger UI)
- **HTTPException** for error responses

---

## API Authentication

### OwnTracks Authentication

OwnTracks HTTP ingestion is handled by `/api/owntracks`, `/api/owntracks/`, `/api/pub`, and
`/api/pub/`. OwnTracks requests must use both:

```python
# Headers: Authorization: Basic base64(OWNTRACKS_USERNAME:OWNTRACKS_PASSWORD)
# Body: {"_type":"encrypted","data":"..."} encrypted with OWNTRACKS_ENCRYPTION_KEY
```

The encryption key must be 32 UTF-8 bytes or fewer. The server pads it to libsodium SecretBox's
32-byte key size, decrypts the OwnTracks `data` value, then passes the original JSON payload into
the existing OwnTracks parser. Plaintext OwnTracks HTTP payloads are rejected, and the endpoint
fails closed when `OWNTRACKS_ENCRYPTION_KEY` is not configured.

### Non-OwnTracks API Authentication

Every other `/api/*` route requires the separate `WEB_API_KEY` through:

```text
Authorization: Bearer <WEB_API_KEY>
```

The only non-OwnTracks exception is `/api/health`, which stays unauthenticated for internal
container health checks. Do not reuse `OWNTRACKS_ENCRYPTION_KEY` as `WEB_API_KEY`.

### Protecting Endpoints

```python
from trip_tracker.api.deps import verify_owntracks_auth
from trip_tracker.services.owntracks import process_owntracks_payload

@router.post("/owntracks")
async def owntracks_http(request: Request) -> JSONResponse:
    verify_owntracks_auth(request)  # Raises HTTPException(401) if Basic Auth fails
    # decrypt and validate the encrypted OwnTracks payload
    with get_owntracks_session_factory()() as db:
        process_owntracks_payload(db, body)
```

OwnTracks ingestion is HTTP-only. Do not add a normal `Depends(get_db)` dependency to these routes;
use the dedicated session factory so PostgreSQL failures can be translated into a retryable `503`.
Authenticate, decrypt, validate, verify Alembic migrations, and commit the raw payload before
returning `200 []`. When PostgreSQL or migrations are unavailable, return `503 Service
Unavailable` with `Retry-After: 30` and `Cache-Control: no-store`. Exact HTTP retries must reuse the
existing raw event rather than inserting a duplicate. Do not add a server-side buffer or MQTT path.

The `/api/health` endpoint is unauthenticated inside the app for container health checks;
`/api/owntracks` requires authentication.

In Docker deployment, the public web service only forwards OwnTracks ingestion endpoints:

- `POST /api/owntracks`
- `POST /api/owntracks/`
- `POST /api/pub`
- `POST /api/pub/`

Other `/api/` routes, `/docs`, `/redoc`, and `/openapi.json` remain reachable only inside the app
container or Docker network unless a future change explicitly reopens them at the web service. Non-OwnTracks
API routes still require `Authorization: Bearer <WEB_API_KEY>` internally. Do not add new
internet-facing API paths without updating `deploy/nginx/default.conf`, docs, and tests.

Custom error pages live in `deploy/nginx/error-pages/` and are copied into the web service image.
When public route behavior changes, keep the configured 4xx/5xx pages visually matched to the app,
unbranded, and written for end users. Browser/static proxy locations should intercept upstream app
errors so missing public page URLs render those custom pages. Do not enable interception on the
OwnTracks API proxy locations unless API clients are intentionally allowed to receive HTML instead
of app JSON errors.

Database-outage web handling is different from normal HTTP errors. The app-level limp-mode
middleware renders `web/templates/limp_mode.html` for browser paths when PostgreSQL is unreachable
and returns HTTP 200 with `X-Trip-Tracker-Limp-Mode: true` so nginx does not replace it with the
generic 503 page. Non-OwnTracks API paths return 503 JSON during limp mode.
Full-page Dashboard and Work Trips requests preflight PostgreSQL reachability and render the
limp-mode page instead of their normal loading shells during an outage. JavaScript content fetches
must receive only `web/templates/_limp_mode_panel.html` so the fetched HTML can replace the shell
without nesting another `layout.html` top bar; shell JavaScript should redirect to `/` if the
content response has `X-Trip-Tracker-Limp-Mode: true` so stale navigation is not left on screen.
While `limp_mode_active` is set, the full outage page hides shared app chrome, navigation, icons,
and service-worker registration. The page uses the end-user facing `Service Temporarily
Unavailable` heading, avoids host/IP/connection-string details and database status cards, and
performs a timed navigation retry to `/` so the normal app/login flow resumes when service returns.
Fragment responses must not include the retry script. OwnTracks HTTP requests receive retryable
`503` responses during the outage so the mobile app retains them for later delivery.

### Credentials Configuration

```env
# .env
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=secret-password
OWNTRACKS_ENCRYPTION_KEY=secret-encryption-key
WEB_API_KEY=separate-web-api-key
```

---

## Adding a New API Endpoint

### Step 1: Define Request/Response Models

Edit [trip_tracker/schemas.py](trip_tracker/schemas.py):

```python
from pydantic import BaseModel, Field
from datetime import date

class MyRequestModel(BaseModel):
    trip_id: int
    custom_field: str = Field(..., min_length=1, max_length=100)

class MyResponseModel(BaseModel):
    status: str
    trip_id: int
    updated_at: str
```

### Step 2: Add the Route

Edit [trip_tracker/api/routes.py](trip_tracker/api/routes.py):

```python
@router.post("/custom-endpoint")
def my_endpoint(
    request: MyRequestModel,
    db: Session = Depends(get_db),
) -> MyResponseModel:
    logger.info("Processing custom request trip_id=%s", request.trip_id)
    
    trip = db.get(Trip, request.trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    # ... process request ...
    
    return MyResponseModel(status="ok", trip_id=trip.id, updated_at=datetime.now().isoformat())
```

### Step 3: Add Authentication (if needed)

For OwnTracks access:
```python
@router.post("/custom-owntracks-endpoint")
def my_endpoint(
    request: Request,
    payload: MyRequestModel,
    db: Session = Depends(get_db),
) -> dict:
    verify_owntracks_auth(request)  # Protect this endpoint
    # ...
```

### Step 4: Test

```bash
# GET (no auth)
curl http://localhost:8000/api/health

# POST with non-OwnTracks API auth
curl -X POST http://localhost:8000/api/custom-endpoint \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${WEB_API_KEY}" \
  -d '{"trip_id": 1, "custom_field": "value"}'

# OwnTracks ingestion uses Basic Auth plus encrypted payloads when configured
curl -X POST http://localhost:8000/api/owntracks \
  -H "Content-Type: application/json" \
  -u "${OWNTRACKS_USERNAME}:${OWNTRACKS_PASSWORD}" \
  -d '{"_type":"encrypted","data":"..."}'
```

---

## Web Routes Structure

### Location

[trip_tracker/web/routes.py](trip_tracker/web/routes.py)

### Trips Page Form Boundary

- Existing rows on `trips.html` intentionally keep the trip date and odometer values read-only.
  The `/trips/{trip_id}` web form accepts only selected `origin_site_id`,
  `destination_site_id`, and mileage edits, so posted `trip_date`, free-text name, or odometer
  values must not move or rewrite those read-only fields.
- Existing row From/To controls are waypoint dropdowns populated from saved `Site` rows. Server
  handlers must validate submitted waypoint IDs and apply the selected waypoint IDs, names, and
  coordinates to the `Trip` without changing the trip's creation source.
- The Add Work Trip form defaults its date input to the app's `LOCAL_TIMEZONE` current date and uses
  the same waypoint dropdown list for the origin and destination. Its service path calculates
  start/end odometers from the latest known odometer reading and resequences that trip plus every
  later trip when a prior-date manual trip is inserted.
- The Diagnostics manual odometer route saves the explicit reading as the master checkpoint only
  when current OwnTracks state is inside the exact `Home` waypoint. Refuse normal saves and disable
  the normal Save button away from Home. At Home, align all trip row odometers so the latest trip
  end matches the reading while preserving stored trip miles and positive between-trip gaps.
  Emergency Rebuild stays available away from Home, creates a full backup first, preserves stored
  distances, repairs or discards invalid gaps, and updates the same master checkpoint. No trip-only
  workflow may update the master checkpoint.
- A trip-distance edit must keep the edited trip's current start odometer and resequence only that
  row and later rows in the same start month. Do not modify earlier rows or rows in another month.
- Dashboard reimbursement summaries must reuse the same monthly trip-mile total, reimbursement
  gallons, monthly gas price, `VEHICLE_MPG`, and manual extra expense total as
  `generate_monthly_pdf()` so the home card matches the downloadable report. Keep displayed
  reimbursement gallons to one decimal place.
- Dashboard OwnTracks Events and Work Trips count cards are current-month cards. Scope them with
  the app-local month bounds from midnight on the first day of the month in `LOCAL_TIMEZONE`;
  month rollover should not delete older month data to make these cards reset. Selected-month
  Work Trips summary cards should use monthly OwnTracks summary rollups when raw OwnTracks rows
  have aged out.
- The Dashboard root route renders a lightweight loading shell. Keep expensive Dashboard queries in
  `/dashboard/content` and render `dashboard_content.html` there so direct homepage loads can show
  the loading state before calculated cards arrive.
- In `dashboard_content.html`, keep Location State as the first visible home card before the other
  stat cards and distance summary cards. Full-width Dashboard stat cards and distance cards should
  use the same compact sizing as the Work Trips selected-month cards while still spanning the app
  width by row; mobile should continue stacking those cards one per row. Do not add a separate
  Dashboard title, description, or header divider above those cards. Keep app-local time in a
  footer below the Recent Work Trips panel.
- The Trips root route renders a lightweight loading shell. Keep selected-month Work Trips queries,
  summary cards, forms, work trip rows, extra report expense rows, and deleted-trip rows in
  `/trips/content` and render `trips_content.html` there so direct Work Trips loads can show a
  loading state before month data arrives.
- `layout.html` keeps authenticated navigation in the shared top bar. Desktop nav links use one
  centered blue raised button treatment, with icons shown to the left of text labels. The
  authenticated header brand uses the cleaned transparent logo asset and shows the current app
  version as a small readable line directly under the Trip Tracker title, while installable app
  icons use launcher-safe padding from the transparent logo and the favicon stays on the square
  original logo. When icon assets change, update the static icon cache-busting query in
  `layout.html` and `manifest.webmanifest`. On mobile, CSS hides the brand/icon and keeps nav links
  in one full-width icon-only blue top-bar row instead of using a fixed bottom nav. App
  buttons and button-style links should stay raised, brighten on hover, and press inward when
  clicked while preserving non-navigation button colors. Keep the login page free of shared top
  navigation, visible or metadata app names, app logos, manifest links, favicon links, and Apple
  touch icon links. Keep the mobile viewport non-edge-to-edge, and preserve the manifest browser
  fallback on authenticated app pages so phone system navigation remains visible. The brand
  icon/text is display-only and not a home link.
- The active app color palette lives in `trip_tracker/web/static/styles.css`. Palette samples are
  saved in `docs/design/color-palettes.svg`; Option A is the current palette. Approved Monthly Work
  Trips row colors are blue (`#4BA3FF`) for automatic trips, purple (`#A855F7`) for edited trips,
  and gold (`#E2AD45`) for manual trips. Do not apply a new app-wide palette until the user chooses
  one. Palette changes must keep `styles.css`, nginx error pages, `theme-color` metadata,
  `manifest.webmanifest`, and the app icon visually coordinated.
- `trips.html` uses a single native month/year picker for the selected report month. It should
  default to the current local month, auto-load the chosen month, and show the month as
  `Showing June 2026 (06/2026)` style text under the page title. Keep the title and selected-month
  description on the left and the selector and PDF download controls on the right in one compact,
  bottom-aligned desktop row directly above the header divider. Collapse the groups vertically on
  narrow screens instead of squeezing or clipping them.
- Dashboard, Waypoints, and Diagnostics start directly with their functional content and do not
  render separate top-level page titles, descriptions, or dividers. Keep the OwnTracks waypoint
  export action in a footer below the saved-waypoint list.
- `trips_content.html` shows compact selected-month cards directly below the month selector rule.
  Keep these scoped to the selected month: work trips plus non-work trips, work trips only,
  OwnTracks events by captured time, work trip count, reimbursement, and monthly average gas price.
- Monthly Work Trips rows use full-row tinting: unedited automatic rows use subtle blue, edited
  non-manual rows use purple, and only trips created from Add Work Trip use subtle gold. Do not add
  an Edited pill. Keep the three-color explanation key below the list. Deleted Work Trip Records
  continue using creation-source blue and gold tinting.
- The Work Trips page order is selected-month cards, Monthly Work Trips, Add Work Trip, Extra
  Report Expenses, then Deleted Work Trip Records.
- The extra report expenses card sits above Deleted Work Trip Records. It accepts a date, expense
  reason, and price, stores expenses by the submitted date's report month, enforces a hard cap of
  five expenses per month server-side, and uses non-colliding routes under
  `/trips/report-expenses/...` because `/trips/{trip_id}` already owns two-segment POST paths.
- Diagnostics hard drive space rows group configured runtime paths as the same drive only when
  exact used bytes and total bytes both match. Keep this grouping rule aligned with the visible
  drive-space bars and database summary in `diagnostics.html`. The Diagnostics Application card
  also shows the source-controlled app version from `trip_tracker.__version__`.
- The Diagnostics Data card shows record counts and the lowest/highest queried gas price readings
  from `gas_price_snapshots.price_per_gallon`. Do not use `MonthlyGasPrice` averages for these
  high/low values.
- Diagnostics degraded/unavailable banners and Pushover app-health notifications must use
  `trip_tracker.services.app_health.build_app_health_snapshot()`. Keep monitored signals aligned:
  PostgreSQL availability/latency, runtime free disk space, active web-login lockouts, and
  app-managed Cloudflare IP blocks. Diagnostics shows each current database latency reading, but
  Pushover must wait `APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS` and confirm latency is still high.
  Disk health uses `APP_HEALTH_DISK_WARNING_FREE_MB` and `APP_HEALTH_DISK_CRITICAL_FREE_MB`, not
  used-percentage alarm thresholds. Preserve immediate issue-change and restoration alerts, and
  repeat unchanged degraded or unavailable states every
  `APP_HEALTH_REMINDER_INTERVAL_SECONDS` (one hour by default).

### Basic Pattern

```python
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=["web/templates"])

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> TemplateResponse:
    # Protect page with login
    await authenticate_web_credentials(request)
    
    trips = db.scalars(select(Trip).limit(10)).all()
    
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"trips": trips, "title": "Dashboard"},
    )
```

### Jinja2 Template Filters

Custom filters are registered in [web/routes.py](trip_tracker/web/routes.py):

```python
def _format_local_datetime(value, fmt: str = "%Y-%m-%d %I:%M:%S %p") -> str:
    return datetime_to_local(value).strftime(fmt)

templates.env.filters["local_datetime"] = _format_local_datetime

# Use in template: {{ trip.started_at | local_datetime }}
```

---

## Adding a New Web Page

### Step 1: Create Jinja2 Template

Create file: `trip_tracker/web/templates/my_page.html`

```html
{% extends "layout.html" %}

{% block content %}
<div class="container">
    <h1>My Page</h1>
    <ul>
    {% for trip in trips %}
        <li>{{ trip.trip_date }} — {{ trip.origin_display_name }} → {{ trip.destination_display_name }}</li>
    {% endfor %}
    </ul>
</div>
{% endblock %}
```

### Step 2: Add the Route

Edit [trip_tracker/web/routes.py](trip_tracker/web/routes.py):

```python
@router.get("/my-page", response_class=HTMLResponse)
def my_page(
    request: Request,
    db: Session = Depends(get_db),
) -> TemplateResponse:
    # Require login if enabled
    if web_login_enabled():
        authenticate_web_credentials(request)
    
    trips = db.scalars(select(Trip)).all()
    
    return templates.TemplateResponse(
        request,
        "my_page.html",
        {"trips": trips},
    )
```

### Step 3: Add Navigation Link

Edit `trip_tracker/web/templates/layout.html`:

```html
<nav>
    <a href="/">Dashboard</a>
    <a href="/trips">Trips</a>
    <a href="/my-page">My Page</a>  <!-- Add link -->
</nav>
```

---

## Web Login Authentication

### Overview

Session-based authentication with optional IP allowlist:

```env
# Enable login
SECRET_KEY=generate-a-long-random-value
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=secret

# Optional: Restrict by IP (CIDR notation)
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24

# Optional: override WebAuthn relying-party settings for passkeys
PASSKEY_RP_NAME=Trip Tracker
PASSKEY_RP_ID=mileage.example.com
PASSKEY_ORIGIN=https://mileage.example.com

# For local testing (disable HTTPS cookie)
WEB_SESSION_COOKIE_SECURE=false
```

### Protecting Routes

```python
from trip_tracker.web.auth import authenticate_web_credentials

@router.get("/protected-page")
def protected_page(request: Request) -> TemplateResponse:
    # Raises HTTPException(403) if not authenticated
    authenticate_web_credentials(request)
    return templates.TemplateResponse(request, "protected.html", {})
```

### Login Flows

1. User visits `/` → redirected to `/login` if not authenticated
2. User enters credentials
3. System checks against `WEB_LOGIN_USERNAME` / `WEB_LOGIN_PASSWORD`
4. On match: Session cookie set, user allowed access
5. Failed attempts: Temporary lockout (`WEB_LOGIN_MAX_ATTEMPTS` x `WEB_LOGIN_LOCKOUT_SECONDS`)
   and a structured audit record written to PostgreSQL
6. Invalid username/password and lockout browser form responses render `login.html` with a top
   status-line error and HTTP 200 so public browser error pages do not replace the form
7. Lockout rejections are also failed login attempts and must be written to the same audit table
8. Successful login stores a structured PostgreSQL audit row, then clears the
   in-memory consecutive-failure state for that client IP
9. When Cloudflare IP blocking is enabled, the app creates an app-managed Cloudflare zone IP
   Access Rule after `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS` consecutive failures for the
   same client IP. Diagnostics also supports manually entered valid IP addresses with a required
   block reason.
10. Passkey login uses `/passkeys/login/options` and `/passkeys/login/verify` as unauthenticated
   ceremony endpoints for the login page. Registration and deletion stay behind authenticated
   Diagnostics routes under `/diagnostics/passkeys/...`.
11. `This is a public device` applies only to password login. It disables Device Sign-In while
    checked, creates a 15-minute browser-activity session, skips service-worker registration, and
    clears cookies/cache/storage on timeout or logout. Keep its explanation in an accessible
    tooltip that appears when the full checkbox row is hovered or keyboard-focused.

The login audit table must never store the submitted password value. Keep failed-login submitted
username, password length, client IP/header details, user agent, request path, reason, attempt
count, lockout state, and UTC/local timestamps available for Diagnostics. Keep successful-login
submitted username, authentication method, client IP/header details, user agent, request path, and
UTC/local timestamps available for the successful-login table. Successful-login rows should show a
Password or Passkey method pill instead of an account column.
Failed passkey assertions must use the same failed-login audit, lockout, and Cloudflare auto-block
path as invalid passwords; use password length `0` and a passkey-specific reason such as
`invalid_passkey`. Do not expose passkey registration without an authenticated Diagnostics session.
The app has one configured web user, so passkeys are stored for `WEB_LOGIN_USERNAME` in
`passkey_credentials` rather than adding separate user-management flows.
`WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` must be set together. When web login is enabled,
`SECRET_KEY` must be changed from the default `change-me`; production Docker fails closed if the
login credentials or session secret are missing. The bundled web service config is loopback-only and
passes Cloudflare's `CF-Connecting-IP` through when present. The app uses that effective client IP
for login audit rows, lockouts, and Cloudflare auto-block identity.
When rendering Diagnostics from the audit table, use the stored effective client IP for
successful-login and failed-login rows. Failed-login row block buttons must use the same visible,
blockable client IP.
Passkey verification must use the public browser origin. Prefer explicit `PASSKEY_ORIGIN` and
`PASSKEY_RP_ID` for unusual reverse-proxy setups; otherwise the passkey service may derive them
from the browser `Origin` header or trusted proxy scheme/host headers. Public passkey use requires
HTTPS except for localhost testing.
Keep `CLOUDFLARE_IP_BLOCK_ALLOWLIST` checks in front of both automatic and manual block actions so
trusted IPs/CIDRs cannot be blocked by this app.

---

## Form Handling

### GET Form Submission

```html
<!-- templates/my_form.html -->
<form method="get" action="/search">
    <input type="text" name="q" placeholder="Search">
    <button type="submit">Search</button>
</form>
```

```python
@router.get("/search")
def search(
    request: Request,
    q: str = Query(...),
    db: Session = Depends(get_db),
) -> TemplateResponse:
    results = db.scalars(
        select(Trip).where(Trip.origin_name.ilike(f"%{q}%"))
    ).all()
    return templates.TemplateResponse(request, "search_results.html", {"results": results})
```

### POST Form Submission

```html
<!-- templates/create_trip.html -->
<form method="post" action="/trips">
    <input type="date" name="trip_date" required>
    <select name="origin_site_id" required>...</select>
    <select name="destination_site_id" required>...</select>
    <input type="number" name="miles" step="0.1" required>
    <button type="submit">Add Work Trip</button>
</form>
```

```python
from fastapi import Form

@router.post("/trips")
async def create_trip(
    request: Request,
    trip_date: date = Form(...),
    origin_site_id: int = Form(...),
    destination_site_id: int = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    origin_site = _load_trip_form_waypoint(db, origin_site_id)
    destination_site = _load_trip_form_waypoint(db, destination_site_id)
    trip = create_manual_trip(
        db,
        trip_date=trip_date,
        origin_name=origin_site.name,
        destination_name=destination_site.name,
        miles=miles,
    )
    _apply_trip_waypoints(trip, origin_site, destination_site)
    return RedirectResponse(url="/trips", status_code=303)
```

---

## Database Session Management

### Getting a Session

```python
from trip_tracker.database import get_db

@router.get("/trips")
def list_trips(db: Session = Depends(get_db)):
    trips = db.scalars(select(Trip)).all()
    return trips
```

The `get_db()` dependency automatically:
- Creates a session
- Commits on success
- Rolls back on exception
- Closes connection

### Lazy Loading

SQLAlchemy relationships can lazy-load from templates. To avoid N+1 queries, use `joinedload`:

```python
from sqlalchemy.orm import joinedload

@router.get("/trips")
def list_trips(db: Session = Depends(get_db)):
    trips = db.scalars(
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
    ).unique().all()
    return trips
```

---

## Response Types

### JSON

```python
from fastapi import APIRouter

@router.get("/api/trips")
def get_trips(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": t.id, "miles": str(t.miles)} for t in db.scalars(select(Trip))]
```

### HTML

```python
from fastapi.responses import HTMLResponse

@router.get("/trips", response_class=HTMLResponse)
def trips_page(request: Request, db: Session = Depends(get_db)) -> TemplateResponse:
    return templates.TemplateResponse(request, "trips.html", {})
```

### File Download

```python
from fastapi.responses import FileResponse

@router.get("/export/pdf")
def export_pdf(db: Session = Depends(get_db)) -> Response:
    pdf = generate_monthly_pdf(db, 2026, 6)
    return Response(
        content=pdf.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )
```

Monthly PDF downloads use `REPORT_DISPLAY_NAME` only as an optional header identity line. When set,
`generate_monthly_pdf()` renders it under the title as `Submitted by:`. Keep this value out of the
download filename and out of reimbursement calculations.
Keep generated PDF reports in portrait letter layout unless the user explicitly requests another
page orientation. The portrait report uses condensed margins so the trip table has more usable
width and height. Keep the title using the selected report month name and year, such as
`Mileage & Expense Report - June 2026`. Keep the title, optional submitted-by identity line, and
trip table tightly stacked at the top of the report. Manual extra expense rows render after trip
rows with date, a wide reason cell, and right-aligned price; the summary includes extra expense
total immediately above the final total reimbursement row. Keep extra expense rows unhighlighted
like trip rows; only the final total reimbursement value cell should use the yellow highlight.

### Redirects

```python
from fastapi.responses import RedirectResponse

@router.post("/login")
async def login(credentials: LoginRequest) -> RedirectResponse:
    # ... verify credentials ...
    return RedirectResponse(url="/dashboard", status_code=303)
```

---

## Logging

Use named loggers for different modules:

```python
import logging

logger = logging.getLogger(__name__)  # Gets "trip_tracker.api.routes"

@router.post("/trips/{trip_id}")
def update_trip(...):
    logger.info("Updated trip trip_id=%s miles=%s", trip_id, update.miles)
    # Collected from container stdout/stderr
```

All application logging is console-only so Docker Compose and Docker Swarm can own retention and
delivery. Do not add `FileHandler`, `RotatingFileHandler`, or Diagnostics app-log panels/downloads.

The Diagnostics page reads `web_login_audits` through
`trip_tracker.services.login_failures.tail_login_success_entries()` and
`tail_login_failure_entries()`. When changing login, diagnostics, or web authentication behavior,
preserve the successful-login table above the failed-login table, the failed-login table actions,
and the compatibility `/diagnostics/logs/login-failures` database-backed JSON Lines export endpoint.
Individual failed-login rows may be hidden from the Diagnostics table through
`hidden_login_failures`, but the source audit row must remain intact. Keep the Diagnostics card actions scoped to the
individual failed-login rows rather than adding separate footer refresh or download buttons.
The Configure Passkey Diagnostics card creates one new WebAuthn credential at a time, lists stored
credentials, and removes only the selected `passkey_credentials` row. Keep the top Diagnostics
cards grouped together in this order unless the page is reorganized deliberately: Application,
System Status, Data, Latest Records, OwnTracks State, Manual Odometer, EIA API, Configure
Passkey, and Hard Drive Space. The System Status card uses
`trip_tracker.services.runtime_status.build_runtime_status()` for PostgreSQL local/remote
placement, and route-level Diagnostics helpers add
safe latency, database size, total app records, pool, and timeout details without exposing full
connection strings. Database latency should render with the same status-dot pattern as other
System Status rows, using green below `APP_HEALTH_DB_LATENCY_WARNING_MS`, yellow at or above the
warning threshold or when unavailable, and red at or above `APP_HEALTH_DB_LATENCY_CRITICAL_MS`.
The app-health banner appears above this card group only when the shared snapshot reports warning
or critical issues.
Cloudflare block/unblock controls should only create and remove app-managed rows in
`cloudflare_ip_blocks`; do not touch unrelated Cloudflare rules. Validate manual IP entries before
calling Cloudflare, require a block reason, show each reason in the blocked-IP table, and keep each
row's remove action deleting both the Cloudflare rule and the local row. The failed-login row block
button must post the effective client IP shown in the failed-login Client IP column.
Automatic blocks should also record a reason, and the blocked-IP table should render an Auto or
Manual source pill for each block. Cloudflare API error `10000` means the configured API credential
was rejected; keep the user message pointed at `CLOUDFLARE_API_TOKEN`, the
`Account Firewall Access Rules Write` permission, and the distinction from
`CLOUDFLARED_TUNNEL_TOKEN` and Global API Keys. Keep the Diagnostics successful-login table,
failed-login table, Cloudflare blocked-IP table, recent OwnTracks entries, and OwnTracks
state-change log paginated at 10 visible rows per page so the cards stay compact.
Their mobile pagination controls should keep First, Previous, Next, and Last in one full-width row
with the page count rendered as plain text below the buttons. Keep pagination links progressively
enhanced so First, Previous, Next, and Last replace only the targeted list and preserve scroll
position when JavaScript is available, while still working as normal links without JavaScript.
The Recent OwnTracks Entries table should show original event time, capture-to-receive delay, and
readable event labels instead of the database row ID, raw receive timestamps, or battery level.
The OwnTracks state-change table should keep per-segment distance out of the list and show original
event time, received delay, state, waypoint, source, elapsed duration since the prior state change,
and the event row's rolling odometer when available.

### Diagnostics Full Backup And Restore

Diagnostics exposes full app data backup and restore through:

- `GET /diagnostics/backup`
- `GET /diagnostics/automatic-backups/download?filename=...`
- `POST /diagnostics/restore`
- `POST /diagnostics/automatic-backups/restore`
- `trip_tracker.services.backups`

These routes are sensitive because backups contain location history and restore replaces current
app rows. Keep them behind configured web login, keep `Cache-Control: no-store` on backup
downloads, validate retained automatic-backup filenames before reading files, validate the uploaded
backup before deleting current rows, and require the typed confirmation value `RESTORE` for upload
and automatic-backup restore forms. Keep the manual full-backup download copy and button with the
lower upload-restore controls rather than in the card header. Automatic backups run once at app
startup and then every 6 hours when `AUTOMATIC_BACKUPS_ENABLED=true`, are stored in
`AUTOMATIC_BACKUP_DIR` on the dedicated `HOST_BACKUP_DIR` bind mount, and retain the newest 4
recent backups plus one daily backup for each of the prior 2 days. Storage failures, including
stale file handles, retry every `AUTOMATIC_BACKUP_RETRY_SECONDS` until successful before the
6-hour schedule resumes. Startup-created automatic backups use the startup filename prefix and
Diagnostics labels them as Startup. The backup format is gzip-compressed JSON of all SQLAlchemy app
tables plus an OwnTracks waypoint export; it is not a raw PostgreSQL cluster, Docker volume, role,
password, or host-log backup. Keep retained automatic-backup rows single-line friendly by
truncating long filenames visually and avoiding a visible confirmation label in each row; the typed
`RESTORE` field should still keep an accessible label.

---

## Error Handling

### HTTPException

```python
from fastapi import HTTPException

@router.get("/trips/{trip_id}")
def get_trip(trip_id: int, db: Session = Depends(get_db)):
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip
```

### Custom Exception Handlers

```python
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )
```

---

## Configuration and Environment

All routes can access settings:

```python
from trip_tracker.config import get_settings

@router.get("/config")
def get_config() -> dict[str, str]:
    settings = get_settings()
    return {
        "vehicle_mpg": str(settings.vehicle_mpg),
        "local_timezone": settings.local_timezone,
    }
```

---

## Testing Routes

### API Tests

```python
from fastapi.testclient import TestClient
from trip_tracker.app import app

client = TestClient(app)

def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_owntracks_requires_auth():
    response = client.post("/api/owntracks", json={})
    assert response.status_code == 401
```

### Web Tests

See [tests/test_web.py](tests/test_web.py) for examples of testing:
- Page rendering
- Form submissions
- Login flows
- Database queries

---

## References

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Jinja2 Template Documentation](https://jinja.palletsprojects.com/)
- [api/routes.py](trip_tracker/api/routes.py) — Existing API endpoints
- [web/routes.py](trip_tracker/web/routes.py) — Existing web routes
- [web/templates/](trip_tracker/web/templates/) — Template examples
- [schemas.py](trip_tracker/schemas.py) — Request/response models
