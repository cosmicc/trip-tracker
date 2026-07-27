#!/usr/bin/env bash
set -Eeuo pipefail

target="${1:-.env}"
template=".env.docker.example"

if [[ -f "${target}" ]]; then
  echo "${target} already exists. Refusing to overwrite it." >&2
  exit 1
fi

if [[ ! -f "${template}" ]]; then
  echo "Missing ${template}" >&2
  exit 1
fi

python3 - "${template}" "${target}" <<'PY'
from pathlib import Path
import secrets
import sys

template = Path(sys.argv[1])
target = Path(sys.argv[2])

replacements = {
    "change-me": secrets.token_hex(32),
    "change-web-login-password": secrets.token_urlsafe(32),
    "change-web-api-key": secrets.token_urlsafe(32),
    "change-postgres-password": secrets.token_urlsafe(32),
    "change-owntracks-password": secrets.token_urlsafe(32),
    "change-owntracks-encryption-key": secrets.token_urlsafe(24),
}

content = template.read_text()
for old, new in replacements.items():
    content = content.replace(old, new)

target.write_text(content)
PY

chmod 0600 "${target}"

if ! ./scripts/prepare_host_directories.sh "${target}"; then
  echo "The .env file was created, but its host mount directories still need preparation." >&2
  printf 'Run: sudo ./scripts/prepare_host_directories.sh %q\n' "${target}" >&2
fi

echo "Created ${target}. Review it, then run: docker compose up -d --build"
