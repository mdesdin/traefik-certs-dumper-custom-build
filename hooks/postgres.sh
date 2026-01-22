#!/bin/ash
set -eu

# -----------------------------
# Helpers
# -----------------------------
log() { echo "$@"; }
warn() { echo "WARN: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

require_env() {
  var="$1"
  val="$(eval "printf '%s' \"\${$var:-}\"")"
  [ -n "$val" ] || die "Missing required environment variable: $var"
}

post_discord() {
  # Optional: DISCORD_WEBHOOK
  [ -n "${DISCORD_WEBHOOK:-}" ] || return 0

  # Minimal JSON escaping for username/content (quotes + backslashes)
  # (Good enough for simple text messages.)
  _u="${DISCORD_USERNAME:-PostgreSQL}"
  _c="$1"

  esc() {
    # escape backslash and double-quote
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
  }

  payload="{\"username\":\"$(esc "$_u")\",\"content\":\"$(esc "$_c")\"}"

  if [ "${DRY_RUN:-0}" = "1" ]; then
    log "[dry-run] Would POST to Discord webhook"
    log "[dry-run] Payload: $payload"
    return 0
  fi

  # Discord webhooks expect application/json
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$DISCORD_WEBHOOK" >/dev/null || warn "Discord webhook POST failed"
}

# -----------------------------
# Required environment
# -----------------------------
require_env DEST_DIR
require_env POSTGRES_PASSWORD
require_env POSTGRESQL_HOST

DEST_DIR="${DEST_DIR%/}"  # strip trailing slash if present

# Optional overrides
PGUSER="${POSTGRES_USER:-postgres}"
PGDATABASE="${POSTGRES_DB:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
PSQL_BIN="${PSQL_BIN:-/usr/bin/psql}"

# -----------------------------
# Validate DEST_DIR exists
# -----------------------------
[ -d "$DEST_DIR" ] || die "DEST_DIR does not exist or is not a directory: $DEST_DIR"

log "Reloading PostgreSQL config..."
post_discord "Reloading PostgreSQL config..."

# -----------------------------
# Permissions: make readable by postgres user
# Strategy:
# - chgrp -R postgres on DEST_DIR
# - grant group read on files
# - grant group execute (traverse) on dirs
#
# This works for BOTH directory structures used by traefik-certs-dumper and any similar nesting.
# -----------------------------
if [ "${DRY_RUN:-0}" = "1" ]; then
  log "[dry-run] Would chgrp -R postgres \"$DEST_DIR\""
  log "[dry-run] Would chmod g+rx on directories under \"$DEST_DIR\""
  log "[dry-run] Would chmod g+r on files under \"$DEST_DIR\""
else
  # Change group ownership so postgres user (in postgres group) can read via group perms
  chgrp -R postgres "$DEST_DIR"

  # Ensure directories are traversable by group
  find "$DEST_DIR" -type d -exec chmod g+rx {} \;

  # Ensure files are readable by group
  find "$DEST_DIR" -type f -exec chmod g+r {} \;
fi

# -----------------------------
# Reload PostgreSQL config
# -----------------------------
# Use a URI; URL-encode password is not handled here.
# We’ll use PGPASSWORD to avoid URI encoding issues.
export PGPASSWORD="$POSTGRES_PASSWORD"

PGURI="postgresql://${PGUSER}@${POSTGRESQL_HOST}:${PGPORT}/${PGDATABASE}"

if [ "${DRY_RUN:-0}" = "1" ]; then
  log "[dry-run] Would run: $PSQL_BIN \"$PGURI\" -c \"SELECT pg_reload_conf();\""
  exit 0
fi

"$PSQL_BIN" "$PGURI" -c "SELECT pg_reload_conf();" >/dev/null
log "PostgreSQL reload requested (pg_reload_conf)."
