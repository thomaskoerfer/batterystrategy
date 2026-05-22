#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="${1:-root@krfer.de}"
ENV_FILE="/etc/docker-volume-webdav-backup.env"

printf '[local] Provisioning WebDAV Docker-volume backup on %s\n' "$TARGET_HOST"

ssh -o BatchMode=yes "$TARGET_HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; aborting" >&2
  exit 1
fi

log "Installing required packages (rclone, zstd, tar, coreutils)"
apt-get update -qq
apt-get install -y -qq rclone zstd tar coreutils >/dev/null

log "Writing/refreshing protected environment file"
if [ ! -f /etc/docker-volume-webdav-backup.env ]; then
  install -m 600 /dev/null /etc/docker-volume-webdav-backup.env
  cat > /etc/docker-volume-webdav-backup.env <<'ENV'
# Required WebDAV settings
WEBDAV_URL="https://webdav.hidrive.ionos.com/users/USERNAME"
WEBDAV_USER="your-user"
WEBDAV_PASSWORD="your-password"
WEBDAV_VENDOR="other"

# Remote destination within WebDAV
REMOTE_PATH="docker-volume-backups"
# Unencrypted destination for restore bundle artifacts
REMOTE_PLAIN_PATH="docker-restore-bundle"

# Encryption layer for remote storage (rclone crypt)
# Generate strong secrets, for example:
#   openssl rand -base64 32
RCLONE_CRYPT_PASSWORD="replace-with-random-secret"
# Optional second secret (salt); recommended:
#   openssl rand -base64 32
RCLONE_CRYPT_PASSWORD2=""

# Backup retention settings
LOCAL_KEEP_DAYS="7"
REMOTE_KEEP_DAYS="30"
REMOTE_KEEP_COUNT="3"
# Keep local archive after successful upload: 1=yes, 0=delete immediately
KEEP_LOCAL_COPY="0"

# Local staging area
LOCAL_STAGING_DIR="/var/backups/docker-volumes"

# Include /srv as archive in each backup package
INCLUDE_SRV="1"

# Include /root/.wg-easy as archive (wg-easy config/keys)
INCLUDE_ROOT_WG_EASY="1"

# Set to 1 to stop running containers during backup for best consistency.
# This causes downtime during backup.
STOP_CONTAINERS="0"
ENV
else
  chmod 600 /etc/docker-volume-webdav-backup.env
  grep -q '^RCLONE_CRYPT_PASSWORD=' /etc/docker-volume-webdav-backup.env || echo 'RCLONE_CRYPT_PASSWORD="replace-with-random-secret"' >> /etc/docker-volume-webdav-backup.env
  grep -q '^RCLONE_CRYPT_PASSWORD2=' /etc/docker-volume-webdav-backup.env || echo 'RCLONE_CRYPT_PASSWORD2=""' >> /etc/docker-volume-webdav-backup.env
  grep -q '^INCLUDE_SRV=' /etc/docker-volume-webdav-backup.env || echo 'INCLUDE_SRV="1"' >> /etc/docker-volume-webdav-backup.env
  grep -q '^INCLUDE_ROOT_WG_EASY=' /etc/docker-volume-webdav-backup.env || echo 'INCLUDE_ROOT_WG_EASY="1"' >> /etc/docker-volume-webdav-backup.env
  grep -q '^REMOTE_PLAIN_PATH=' /etc/docker-volume-webdav-backup.env || echo 'REMOTE_PLAIN_PATH="docker-restore-bundle"' >> /etc/docker-volume-webdav-backup.env
  grep -q '^KEEP_LOCAL_COPY=' /etc/docker-volume-webdav-backup.env || echo 'KEEP_LOCAL_COPY="0"' >> /etc/docker-volume-webdav-backup.env
  grep -q '^REMOTE_KEEP_COUNT=' /etc/docker-volume-webdav-backup.env || echo 'REMOTE_KEEP_COUNT="3"' >> /etc/docker-volume-webdav-backup.env
fi

log "Installing backup script"
cat > /usr/local/sbin/docker-volume-webdav-backup <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/docker-volume-webdav-backup.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

# shellcheck source=/etc/docker-volume-webdav-backup.env
source "$ENV_FILE"

: "${WEBDAV_URL:?WEBDAV_URL is required}"
: "${WEBDAV_USER:?WEBDAV_USER is required}"
: "${WEBDAV_PASSWORD:?WEBDAV_PASSWORD is required}"
: "${RCLONE_CRYPT_PASSWORD:?RCLONE_CRYPT_PASSWORD is required}"

WEBDAV_VENDOR="${WEBDAV_VENDOR:-other}"
REMOTE_PATH="${REMOTE_PATH:-docker-volume-backups}"
LOCAL_KEEP_DAYS="${LOCAL_KEEP_DAYS:-7}"
REMOTE_KEEP_DAYS="${REMOTE_KEEP_DAYS:-30}"
REMOTE_KEEP_COUNT="${REMOTE_KEEP_COUNT:-3}"
KEEP_LOCAL_COPY="${KEEP_LOCAL_COPY:-0}"
LOCAL_STAGING_DIR="${LOCAL_STAGING_DIR:-/var/backups/docker-volumes}"
STOP_CONTAINERS="${STOP_CONTAINERS:-0}"
INCLUDE_SRV="${INCLUDE_SRV:-1}"
INCLUDE_ROOT_WG_EASY="${INCLUDE_ROOT_WG_EASY:-1}"
RCLONE_CRYPT_PASSWORD2="${RCLONE_CRYPT_PASSWORD2:-}"

TS="$(date +%F_%H-%M-%S)"
HOST="$(hostname -s)"
WORK_DIR="${LOCAL_STAGING_DIR}/work-${TS}"
SNAPSHOT_DIR="${WORK_DIR}/snapshot"
ARCHIVE="${LOCAL_STAGING_DIR}/docker-volumes-${HOST}-${TS}.tar.zst"
RCLONE_CFG="${WORK_DIR}/rclone.conf"
RUNNING_IDS_FILE="${WORK_DIR}/running-containers.txt"
PERM_REPORT="${SNAPSHOT_DIR}/paperless-vsftpd-permissions.txt"
NETWORK_REPORT="${SNAPSHOT_DIR}/docker-network-report.txt"
NETWORK_JSON="${SNAPSHOT_DIR}/docker-network-inspect.json"

mkdir -p "$SNAPSHOT_DIR"

cleanup() {
  rm -rf "$WORK_DIR"
}

restore_containers() {
  if [ "$STOP_CONTAINERS" = "1" ] && [ -s "$RUNNING_IDS_FILE" ]; then
    echo "Starting previously running containers"
    xargs -r docker start < "$RUNNING_IDS_FILE" >/dev/null || true
  fi
}
trap 'restore_containers; cleanup' EXIT

if [ "$STOP_CONTAINERS" = "1" ]; then
  echo "Stopping running containers for a consistent backup"
  docker ps -q > "$RUNNING_IDS_FILE" || true
  if [ -s "$RUNNING_IDS_FILE" ]; then
    xargs -r docker stop < "$RUNNING_IDS_FILE" >/dev/null
  fi
fi

write_permissions_report() {
  {
    echo "timestamp=${TS}"
    echo "host=${HOST}"
    echo
    echo "## host paths"
    for p in /srv /srv/paperless /srv/paperless/export /srv/paperlessftp /srv/paperlessftp/paperless /root/.wg-easy; do
      if [ -e "$p" ]; then
        stat -c '%n uid=%u gid=%g mode=%a perms=%A' "$p"
      else
        echo "$p MISSING"
      fi
    done
    echo
    echo "## paperless container summary"
    if docker ps -a --format '{{.Names}}' | grep -qx 'paperless_webserver_1'; then
      docker inspect -f 'name={{.Name}} image={{.Config.Image}} user={{.Config.User}}' paperless_webserver_1
      echo "mounts:"
      docker inspect -f '{{range .Mounts}}- {{.Type}} {{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}' paperless_webserver_1
      echo "id inside container:"
      docker exec paperless_webserver_1 sh -lc 'id' 2>/dev/null || true
      echo "consume dir in container:"
      docker exec paperless_webserver_1 sh -lc 'stat -c "%n uid=%u gid=%g mode=%a perms=%A" /usr/src/paperless/consume' 2>/dev/null || true
    else
      echo "paperless_webserver_1 not found"
    fi
    echo
    echo "## vsftpd container summary"
    if docker ps -a --format '{{.Names}}' | grep -qx 'vsftpd'; then
      docker inspect -f 'name={{.Name}} image={{.Config.Image}} user={{.Config.User}}' vsftpd
      echo "mounts:"
      docker inspect -f '{{range .Mounts}}- {{.Type}} {{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}' vsftpd
      echo "selected env (sanitized):"
      docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' vsftpd | grep -E '^(FTP_USER|LOCAL_UMASK|FILE_OPEN_MODE|PASV_ENABLE|PASV_MIN_PORT|PASV_MAX_PORT|PASV_ADDR_RESOLVE|REVERSE_LOOKUP_ENABLE|XFERLOG_STD_FORMAT|PORT_PROMISCUOUS|PASV_PROMISCUOUS)=' || true
      echo "id inside container:"
      docker exec vsftpd sh -lc 'id' 2>/dev/null || true
      echo "ftp root in container:"
      docker exec vsftpd sh -lc 'ls -ldn /home/vsftpd /home/vsftpd/paperless' 2>/dev/null || true
    else
      echo "vsftpd not found"
    fi
  } > "$PERM_REPORT"
}

write_network_report() {
  {
    echo "timestamp=${TS}"
    echo "host=${HOST}"
    echo
    echo "## docker network ls"
    docker network ls
    echo
    echo "## network details"
    for net in $(docker network ls --format '{{.Name}}'); do
      echo "### ${net}"
      docker network inspect "$net" >/dev/null 2>&1 && docker network inspect -f 'name={{.Name}} driver={{.Driver}} scope={{.Scope}} internal={{.Internal}} attachable={{.Attachable}}' "$net"
    done
  } > "$NETWORK_REPORT"

  docker network inspect $(docker network ls --format '{{.Name}}') > "$NETWORK_JSON" 2>/dev/null || true
}

write_permissions_report
write_network_report

VOLUMES="$(docker volume ls -q)"
if [ -z "$VOLUMES" ]; then
  echo "No docker volumes found; nothing to do"
  exit 0
fi

while IFS= read -r VOL; do
  [ -z "$VOL" ] && continue
  echo "Archiving volume: $VOL"
  docker run --rm \
    -v "${VOL}:/volume:ro" \
    -v "${SNAPSHOT_DIR}:/backup" \
    busybox:1.36 \
    sh -c 'cd /volume && tar -cf "/backup/'"${VOL}"'.tar" .'
done <<< "$VOLUMES"

if [ "$INCLUDE_SRV" = "1" ] && [ -d /srv ]; then
  echo "Archiving /srv"
  tar --acls --xattrs --numeric-owner -cpf "${SNAPSHOT_DIR}/srv.tar" -C / srv
fi

if [ "$INCLUDE_ROOT_WG_EASY" = "1" ] && [ -d /root/.wg-easy ]; then
  echo "Archiving /root/.wg-easy"
  tar --acls --xattrs --numeric-owner -cpf "${SNAPSHOT_DIR}/root-wg-easy.tar" -C / root/.wg-easy
fi

{
  echo "timestamp=${TS}"
  echo "host=${HOST}"
  docker volume ls
  echo
  docker volume inspect $(docker volume ls -q)
} > "${SNAPSHOT_DIR}/manifest.txt"

tar --zstd -cpf "$ARCHIVE" -C "$SNAPSHOT_DIR" .

OBSCURED_PASS="$(rclone obscure "$WEBDAV_PASSWORD")"
OBSCURED_CRYPT_PASS="$(rclone obscure "$RCLONE_CRYPT_PASSWORD")"

cat > "$RCLONE_CFG" <<RC
[webdavbackup]
type = webdav
url = ${WEBDAV_URL}
vendor = ${WEBDAV_VENDOR}
user = ${WEBDAV_USER}
pass = ${OBSCURED_PASS}

[cryptbackup]
type = crypt
remote = webdavbackup:${REMOTE_PATH}/${HOST}
filename_encryption = standard
directory_name_encryption = true
password = ${OBSCURED_CRYPT_PASS}
RC

if [ -n "$RCLONE_CRYPT_PASSWORD2" ]; then
  OBSCURED_CRYPT_PASS2="$(rclone obscure "$RCLONE_CRYPT_PASSWORD2")"
  printf 'password2 = %s\n' "$OBSCURED_CRYPT_PASS2" >> "$RCLONE_CFG"
fi

chmod 600 "$RCLONE_CFG"

echo "Uploading archive to encrypted remote cryptbackup:"
rclone --config "$RCLONE_CFG" copy "$ARCHIVE" cryptbackup: --transfers 2 --checkers 4

# local retention
if [ "$KEEP_LOCAL_COPY" = "1" ]; then
  find "$LOCAL_STAGING_DIR" -maxdepth 1 -type f -name 'docker-volumes-*.tar.zst' -mtime "+${LOCAL_KEEP_DAYS}" -delete
else
  rm -f "$ARCHIVE"
fi

# remote retention (on encrypted view)
rclone --config "$RCLONE_CFG" delete cryptbackup: --min-age "${REMOTE_KEEP_DAYS}d"

# remote retention by count (keep newest N files)
if [ "${REMOTE_KEEP_COUNT}" -gt 0 ] 2>/dev/null; then
  mapfile -t _remote_files < <(rclone --config "$RCLONE_CFG" lsf cryptbackup: --files-only | sort)
  if [ "${#_remote_files[@]}" -gt "${REMOTE_KEEP_COUNT}" ]; then
    to_delete=$(( ${#_remote_files[@]} - REMOTE_KEEP_COUNT ))
    for ((i=0; i<to_delete; i++)); do
      rclone --config "$RCLONE_CFG" deletefile "cryptbackup:${_remote_files[$i]}" || true
    done
  fi
fi

echo "Backup finished: $ARCHIVE"
SCRIPT
chmod 700 /usr/local/sbin/docker-volume-webdav-backup

log "Writing systemd service + timer"
cat > /etc/systemd/system/docker-volume-webdav-backup.service <<'SERVICE'
[Unit]
Description=Backup Docker volumes to WebDAV
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/docker-volume-webdav-backup
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
SERVICE

cat > /etc/systemd/system/docker-volume-webdav-backup.timer <<'TIMER'
[Unit]
Description=Weekly Docker volume backup to WebDAV

[Timer]
OnCalendar=Sun *-*-* 03:30:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
TIMER

log "Reloading systemd and enabling timer"
systemctl daemon-reload
systemctl enable --now docker-volume-webdav-backup.timer >/dev/null

log "Current timer status"
systemctl --no-pager --full status docker-volume-webdav-backup.timer | sed -n '1,14p'

log "Provisioning done"
REMOTE

cat <<EONEXT

Next step on $TARGET_HOST:
1) Edit $ENV_FILE and set at least:
   - WEBDAV_URL / WEBDAV_USER / WEBDAV_PASSWORD
   - RCLONE_CRYPT_PASSWORD (and optional RCLONE_CRYPT_PASSWORD2)
2) Test once: systemctl start docker-volume-webdav-backup.service
3) Check logs: journalctl -u docker-volume-webdav-backup.service -n 120 --no-pager

EONEXT
