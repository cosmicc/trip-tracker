#!/usr/bin/env bash
set -Eeuo pipefail

# Restrict newly created bind-mount directories while leaving existing permissions unchanged.
umask 027

env_file="${1:-.env}"

read_env_file_value() {
  local key="$1"

  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi

  python3 - "${env_file}" "${key}" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
key = sys.argv[2]

for line in env_path.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        continue
    name, value = stripped.split("=", 1)
    if name.strip() == key:
        print(value.strip().strip('"').strip("'"))
        break
PY
}

setting_value() {
  local key="$1"
  local default_value="$2"
  local environment_value="${!key-}"
  local file_value=""

  if [[ -n "${environment_value}" ]]; then
    printf '%s\n' "${environment_value}"
    return
  fi

  file_value="$(read_env_file_value "${key}")"
  printf '%s\n' "${file_value:-${default_value}}"
}

prepare_directory() {
  local label="$1"
  local directory_path="$2"

  if mkdir -p -- "${directory_path}"; then
    echo "Prepared ${label}: ${directory_path}"
    return
  fi

  echo "Could not create ${label}: ${directory_path}" >&2
  return 1
}

host_data_dir="$(setting_value HOST_DATA_DIR /var/lib/trip-tracker)"
host_backup_dir="$(setting_value HOST_BACKUP_DIR /var/lib/trip-tracker/backups)"

preparation_failed=false
prepare_directory "host app data directory" "${host_data_dir}" || preparation_failed=true
prepare_directory "host automatic backup directory" "${host_backup_dir}" || preparation_failed=true

if [[ "${preparation_failed}" == "true" ]]; then
  echo "Run this script with an account allowed to create both host mount directories." >&2
  exit 1
fi
