#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=/etc/ha-backup-webdav-sync.env
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE" >&2; exit 1; }
# shellcheck source=/etc/ha-backup-webdav-sync.env
source "$ENV_FILE"

: "${BASE_ENV_FILE:?BASE_ENV_FILE missing}"
[ -f "$BASE_ENV_FILE" ] || { echo "Missing $BASE_ENV_FILE" >&2; exit 1; }
# shellcheck source=/etc/docker-volume-webdav-backup.env
source "$BASE_ENV_FILE"

: "${WEBDAV_URL:?WEBDAV_URL missing}"
: "${WEBDAV_USER:?WEBDAV_USER missing}"
: "${WEBDAV_PASSWORD:?WEBDAV_PASSWORD missing}"

HA_BACKUP_DIR="${HA_BACKUP_DIR:-/var/lib/docker/volumes/homeassistant_config/_data/backups}"
HA_REMOTE_PATH="${HA_REMOTE_PATH:-homeassistant-auto-backups}"
HA_REMOTE_KEEP="${HA_REMOTE_KEEP:-3}"
WEBDAV_VENDOR="${WEBDAV_VENDOR:-other}"
HOST="$(hostname -s)"
REMOTE_DIR="hawebdav:${HA_REMOTE_PATH}/${HOST}"

mkdir -p "$HA_BACKUP_DIR"

LOCK=/run/ha-backup-webdav-sync.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another ha-backup-webdav-sync run is active"
  exit 0
fi

WORK_DIR="$(mktemp -d /tmp/ha-bkp-sync-XXXXXX)"
cleanup(){ rm -rf "$WORK_DIR"; }
trap cleanup EXIT
CFG="$WORK_DIR/rclone.conf"

cat > "$CFG" <<RC
[hawebdav]
type = webdav
url = ${WEBDAV_URL}
vendor = ${WEBDAV_VENDOR}
user = ${WEBDAV_USER}
pass = $(rclone obscure "$WEBDAV_PASSWORD")
RC
chmod 600 "$CFG"

shopt -s nullglob
files=("$HA_BACKUP_DIR"/*.tar)

for f in "${files[@]}"; do
  b="$(basename "$f")"
  echo "Uploading $b"
  rclone --config "$CFG" copyto "$f" "$REMOTE_DIR/$b" --checksum --transfers 2 --checkers 4 --retries 5 --low-level-retries 10
  if rclone --config "$CFG" lsf "$REMOTE_DIR" --files-only | grep -Fxq "$b"; then
    rm -f -- "$f"
    echo "Uploaded and removed local: $b"
  else
    echo "Remote verification failed for $b" >&2
    exit 1
  fi
done

python3 - <<PY
import json, subprocess
cfg = "$CFG"
remote = "$REMOTE_DIR"
keep = int("$HA_REMOTE_KEEP")

p = subprocess.run(["rclone","--config",cfg,"lsjson",remote,"--files-only"], capture_output=True, text=True)
if p.returncode != 0:
    raise SystemExit(p.stderr.strip() or "rclone lsjson failed")
items = json.loads(p.stdout or "[]")
items.sort(key=lambda x: x.get("ModTime", ""), reverse=True)
for it in items[keep:]:
    name = it.get("Name")
    if not name:
        continue
    d = subprocess.run(["rclone","--config",cfg,"deletefile",f"{remote}/{name}"], capture_output=True, text=True)
    if d.returncode != 0:
        raise SystemExit(d.stderr.strip() or f"delete failed: {name}")
    print(f"Deleted remote old backup: {name}")
PY

echo "Remote backups:"
rclone --config "$CFG" lsf "$REMOTE_DIR" --files-only | sed -n '1,20p'
echo "Local backups:"
local_left=( "$HA_BACKUP_DIR"/*.tar )
if [ "${#local_left[@]}" -eq 0 ]; then
  echo "(none)"
else
  printf '%s\n' "${local_left[@]}"
fi
