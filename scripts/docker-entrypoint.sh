#!/usr/bin/env bash
set -Eeuo pipefail

wait_for_database() {
  local timeout="${DB_WAIT_TIMEOUT_SECONDS:-60}"
  python - "$timeout" <<'PY'
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError

from trip_tracker.config import Settings
from trip_tracker.database_engine import database_engine_options, normalized_database_url

timeout = int(sys.argv[1])
settings = Settings()
deadline = time.time() + timeout
last_error = None

while time.time() < deadline:
    try:
        engine = create_engine(
            normalized_database_url(settings.database_url),
            **database_engine_options(settings),
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        raise SystemExit(0)
    except (ArgumentError, ModuleNotFoundError) as exc:
        raise SystemExit(f"Database URL is invalid: {exc}") from exc
    except Exception as exc:
        last_error = exc
        time.sleep(2)

raise SystemExit(f"Database did not become ready within {timeout}s: {last_error}")
PY
}

prepare_runtime_paths() {
  local app_data_dir="${APP_DATA_DIR:-/data}"
  local automatic_backup_dir="${AUTOMATIC_BACKUP_DIR:-${app_data_dir%/}/backups}"

  mkdir -p \
    "${app_data_dir}" \
    "${automatic_backup_dir}"
  chown -R app:app "${app_data_dir}"
  chmod 0750 "${app_data_dir}"
  chmod 0750 "${automatic_backup_dir}"
}

run_as_app() {
  if [[ "$(id -u)" == "0" ]]; then
    exec gosu app "$@"
  fi
  exec "$@"
}

if [[ "$(id -u)" == "0" ]]; then
  prepare_runtime_paths
fi

if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
  if wait_for_database; then
    if [[ "$(id -u)" == "0" ]]; then
      gosu app alembic upgrade head
    else
      alembic upgrade head
    fi
  else
    echo "Database is unavailable; starting the app in read-only outage mode." >&2
    echo "OwnTracks HTTP requests will receive 503 until PostgreSQL returns." >&2
  fi
fi

run_as_app "$@"
