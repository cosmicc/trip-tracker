# Upgrading to Trip Tracker 1.5

Version 1.5.0 renames Mileage Logger to Trip Tracker. The database schema and stored trip data do
not change, but package, container, storage, cookie, and deployment identifiers do.

## Before You Start

1. Download a full application backup from Diagnostics.
2. Create a PostgreSQL dump when possible.
3. Record the current Docker volume, bind-mount, stack, and Cloudflare Tunnel origin names.
4. Stop the old application before renaming the database or moving writable host directories.

Do not run `docker compose down -v`. The `-v` option removes the bundled PostgreSQL volume.

## Name Changes

| Before 1.5.0 | Starting with 1.5.0 |
|---|---|
| Mileage Logger | Trip Tracker |
| `mileage_logger` Python package and database | `trip_tracker` |
| `mileage-logger` package and CLI | `trip-tracker` |
| PostgreSQL role `mileage` | `triptracker` |
| Services `mlapp` and `mlnginx` | `ttapp` and `ttnginx` |
| Network `mileage-internal` | `trip-tracker-internal` |
| Host path `/var/lib/mileage-logger` | `/var/lib/trip-tracker` |
| Session cookie `mileage_logger_session` | `trip_tracker_session` |
| Repository `cosmicc/Mileage-Logger` | `cosmicc/Trip-Tracker` |
| GHCR `mileage-logger-app` and `mileage-logger-nginx` | `trip-tracker-app` and `trip-tracker-nginx` |

The cookie rename signs users out once. Existing passkeys remain valid when `PASSKEY_RP_ID` and
`PASSKEY_ORIGIN` stay unchanged; only the default relying-party display name changes.

## Rename PostgreSQL

Connect to a maintenance database such as `postgres`, not to the database being renamed. End any
remaining connections to the old database, then rename the database and role:

```sql
ALTER DATABASE mileage_logger RENAME TO trip_tracker;
ALTER ROLE mileage RENAME TO triptracker;
```

Reset the renamed role's password with the interactive `psql` command so the new password is not
placed in shell history:

```text
\password triptracker
```

Update the deployment secret without committing it:

```env
POSTGRES_DB=trip_tracker
POSTGRES_USER=triptracker
POSTGRES_PASSWORD=<new-password>
DATABASE_URL=postgresql+psycopg://triptracker:<url-encoded-password>@postgres:5432/trip_tracker
```

`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` initialize only a new PostgreSQL data
directory. Changing those variables does not rename an existing database or role.

## Preserve the Bundled PostgreSQL Volume

Version 1.5.0 adds `POSTGRES_DATA_VOLUME=trip-tracker-postgres-data` so the default volume name is
not coupled to the Compose or Portainer project name.

For an in-place upgrade, first find the existing volume:

```bash
docker volume ls
```

Set `POSTGRES_DATA_VOLUME` to that exact existing volume name for the first 1.5.0 deployment. After
the database is verified, either keep that name or migrate the data to the new
`trip-tracker-postgres-data` volume using a PostgreSQL dump and restore. Never copy a live
PostgreSQL data directory between volumes.

## Move Persistent App Data

Move the stopped application's bind-mounted data and backup directories to
`/var/lib/trip-tracker`, preserve their existing owner and permissions, and update:

```env
HOST_DATA_DIR=/var/lib/trip-tracker
HOST_BACKUP_DIR=/var/lib/trip-tracker/backups
```

Create either missing host directory and all required parents before deploying:

```bash
sudo ./scripts/prepare_host_directories.sh .env
```

The script reads both paths from `.env` and uses `mkdir -p`, so it is safe to rerun when the
directories already exist. On Swarm nodes, run it on every node eligible to host `ttapp`, then
verify the created paths are writable by the configured `APP_UID` and `APP_GID`.

Deployments old enough to keep automatic backups under
`/var/log/mileage-logger/backups` should move those files into the new `HOST_BACKUP_DIR` too.
Trip Tracker continues to recognize pre-1.5 full-backup format markers and retained automatic
backup filenames after those files are moved. New backups use only Trip Tracker names.

## Update Deployment References

- Change the Cloudflare Tunnel origin to `http://ttnginx`.
- Use stack/project name `trip-tracker`.
- Use `ghcr.io/cosmicc/trip-tracker-app:1.5.0` and
  `ghcr.io/cosmicc/trip-tracker-nginx:1.5.0` after those packages are published.
- Update scripts from `docker compose exec mlapp ...` to `docker compose exec ttapp ...`.
- Update Python imports from `mileage_logger` to `trip_tracker` and CLI calls from
  `mileage-logger` to `trip-tracker`.
- Rename the GitHub repository separately, then update the local Git remote URL.

Start the new deployment and verify `/api/health`, login, Diagnostics record counts, automatic
backup visibility, and one report download before removing any old deployment objects.
