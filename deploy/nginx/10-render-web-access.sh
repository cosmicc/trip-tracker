#!/usr/bin/env sh
set -eu

access_file="${WEB_ACCESS_CONF_PATH:-/etc/nginx/includes/web-access.conf}"
allowed_cidrs="${WEB_ALLOWED_CIDRS:-}"

mkdir -p "$(dirname "${access_file}")"

if [ -z "$(printf '%s' "${allowed_cidrs}" | tr -d '[:space:]')" ]; then
    printf 'allow all;\n' > "${access_file}"
    echo "Trip Tracker web UI access: WEB_ALLOWED_CIDRS is blank, allowing all clients"
    exit 0
fi

: > "${access_file}"
has_rules=0

for cidr in $(printf '%s' "${allowed_cidrs}" | tr ',' ' '); do
    if [ "${cidr}" = "all" ]; then
        printf 'allow all;\n' > "${access_file}"
        echo "Trip Tracker web UI access: allowing all clients"
        exit 0
    fi

    if ! printf '%s' "${cidr}" | grep -Eq '^[0-9A-Fa-f:.\/]+$'; then
        echo "Invalid WEB_ALLOWED_CIDRS entry: ${cidr}" >&2
        echo "Use comma-separated CIDR blocks, for example: 192.168.1.0/24,10.8.0.0/24" >&2
        exit 1
    fi

    printf 'allow %s;\n' "${cidr}" >> "${access_file}"
    has_rules=1
done

if [ "${has_rules}" = "0" ]; then
    printf 'allow all;\n' > "${access_file}"
    echo "Trip Tracker web UI access: no CIDR entries found, allowing all clients"
    exit 0
fi

printf 'deny all;\n' >> "${access_file}"
echo "Trip Tracker web UI access: restricted to WEB_ALLOWED_CIDRS=${allowed_cidrs}"
