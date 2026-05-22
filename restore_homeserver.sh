#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="${1:-root@krfer.de}"
BACKUP_SELECTOR="${2:-latest}" # latest or exact filename (e.g. docker-volumes-<host>-<ts>.tar.zst)

printf '[local] Starting restore on %s (backup=%s)\n' "$TARGET_HOST" "$BACKUP_SELECTOR"

ssh -o BatchMode=yes "$TARGET_HOST" "BACKUP_SELECTOR='$BACKUP_SELECTOR' bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ENV_FILE="/etc/docker-volume-webdav-backup.env"
LOCAL_BACKUP_ROOT="/var/backups/docker-volumes"
RESTORE_ROOT="${LOCAL_BACKUP_ROOT}/restore"

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root" >&2
  exit 1
fi

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
HOST="$(hostname -s)"

mkdir -p "$RESTORE_ROOT"
WORK_DIR="$(mktemp -d "${RESTORE_ROOT}/work-XXXXXX")"
SNAPSHOT_DIR="${WORK_DIR}/snapshot"
RCLONE_CFG="${WORK_DIR}/rclone.conf"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

log "Installing required packages"
apt-get update -qq
apt-get install -y -qq rclone zstd tar >/dev/null

log "Preparing temporary rclone config"
cat > "$RCLONE_CFG" <<RC
[webdavbackup]
type = webdav
url = ${WEBDAV_URL}
vendor = ${WEBDAV_VENDOR}
user = ${WEBDAV_USER}
pass = $(rclone obscure "$WEBDAV_PASSWORD")

[cryptbackup]
type = crypt
remote = webdavbackup:${REMOTE_PATH}/${HOST}
filename_encryption = standard
directory_name_encryption = true
password = $(rclone obscure "$RCLONE_CRYPT_PASSWORD")
RC

if [ -n "${RCLONE_CRYPT_PASSWORD2:-}" ]; then
  printf 'password2 = %s\n' "$(rclone obscure "$RCLONE_CRYPT_PASSWORD2")" >> "$RCLONE_CFG"
fi
chmod 600 "$RCLONE_CFG"

if [ "${BACKUP_SELECTOR}" = "latest" ]; then
  log "Selecting latest backup from encrypted remote"
  BACKUP_FILE="$(rclone --config "$RCLONE_CFG" lsf cryptbackup: --files-only | sort | tail -n1)"
  if [ -z "$BACKUP_FILE" ]; then
    echo "No backup file found on crypt remote" >&2
    exit 1
  fi
else
  BACKUP_FILE="${BACKUP_SELECTOR}"
fi

BACKUP_PATH="${RESTORE_ROOT}/${BACKUP_FILE}"
log "Downloading backup ${BACKUP_FILE}"
rclone --config "$RCLONE_CFG" copyto "cryptbackup:${BACKUP_FILE}" "$BACKUP_PATH"

log "Stopping and removing all existing containers"
if [ -n "$(docker ps -q)" ]; then
  docker ps -q | xargs -r docker stop >/dev/null
fi
if [ -n "$(docker ps -aq)" ]; then
  docker ps -aq | xargs -r docker rm >/dev/null
fi

mkdir -p "$SNAPSHOT_DIR"
log "Extracting backup archive"
tar --zstd -xpf "$BACKUP_PATH" -C "$SNAPSHOT_DIR"

if [ -f "${SNAPSHOT_DIR}/srv.tar" ]; then
  log "Restoring /srv"
  if [ -d /srv ]; then
    mv /srv "/srv.pre-restore.$(date +%F_%H-%M-%S)"
  fi
  tar --acls --xattrs --numeric-owner -xpf "${SNAPSHOT_DIR}/srv.tar" -C /
else
  echo "srv.tar missing in backup" >&2
  exit 1
fi

if [ -f "${SNAPSHOT_DIR}/root-wg-easy.tar" ]; then
  log "Restoring /root/.wg-easy"
  rm -rf /root/.wg-easy
  tar --acls --xattrs --numeric-owner -xpf "${SNAPSHOT_DIR}/root-wg-easy.tar" -C /
else
  log "No root-wg-easy.tar in this backup (continuing)"
fi

log "Restoring Docker volumes"
find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name '*.tar' \
  ! -name 'srv.tar' \
  ! -name 'root-wg-easy.tar' \
  ! -name 'manifest.txt' \
  ! -name 'paperless-vsftpd-permissions.txt' \
| while IFS= read -r VOL_TAR; do
  VOL_NAME="$(basename "$VOL_TAR" .tar)"
  [ -z "$VOL_NAME" ] && continue

  docker volume inspect "$VOL_NAME" >/dev/null 2>&1 || docker volume create "$VOL_NAME" >/dev/null

  # Clear existing content to avoid stale files, then restore archive content.
  docker run --rm \
    -v "${VOL_NAME}:/volume" \
    -v "${SNAPSHOT_DIR}:/backup:ro" \
    busybox:1.36 \
    sh -c 'find /volume -mindepth 1 -delete && tar -xf "/backup/'"${VOL_NAME}"'.tar" -C /volume'

  echo "restored volume: ${VOL_NAME}"
done

log "Ensuring proxy network exists"
docker network inspect proxy >/dev/null 2>&1 || docker network create proxy >/dev/null

compose_up() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    log "SKIP compose stack: $dir (missing)"
    return
  fi

  if [ -f "$dir/docker-compose.yml" ] || [ -f "$dir/compose.yml" ] || [ -f "$dir/docker-compose.yaml" ] || [ -f "$dir/compose.yaml" ]; then
    log "Starting compose stack in $dir"
    cd "$dir"
    if command -v docker-compose >/dev/null 2>&1; then
      docker-compose up -d
    else
      docker compose up -d
    fi
  else
    log "SKIP compose stack: $dir (no compose file)"
  fi
}

log "Recreating standalone containers"

# home-assistant
docker run -d \
  --name home-assistant \
  --network proxy \
  --restart unless-stopped \
  -v homeassistant_config:/config \
  -e PATH='/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
  -e LANG='C.UTF-8' \
  -e S6_BEHAVIOUR_IF_STAGE2_FAILS='2' \
  -e S6_CMD_WAIT_FOR_SERVICES_MAXTIME='0' \
  -e S6_CMD_WAIT_FOR_SERVICES='1' \
  -e S6_SERVICES_READYTIME='50' \
  -e S6_SERVICES_GRACETIME='240000' \
  -e UV_EXTRA_INDEX_URL='https://wheels.home-assistant.io/musllinux-index/' \
  -e UV_SYSTEM_PYTHON='true' \
  -e UV_NO_CACHE='true' \
  --cap-add AUDIT_WRITE \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  --cap-add FSETID \
  --cap-add KILL \
  --cap-add MKNOD \
  --cap-add NET_BIND_SERVICE \
  --cap-add NET_RAW \
  --cap-add SETFCAP \
  --cap-add SETGID \
  --cap-add SETPCAP \
  --cap-add SETUID \
  --cap-add SYS_CHROOT \
  --cap-drop AUDIT_CONTROL \
  --cap-drop BLOCK_SUSPEND \
  --cap-drop DAC_READ_SEARCH \
  --cap-drop IPC_LOCK \
  --cap-drop IPC_OWNER \
  --cap-drop LEASE \
  --cap-drop LINUX_IMMUTABLE \
  --cap-drop MAC_ADMIN \
  --cap-drop MAC_OVERRIDE \
  --cap-drop NET_ADMIN \
  --cap-drop NET_BROADCAST \
  --cap-drop SYSLOG \
  --cap-drop SYS_ADMIN \
  --cap-drop SYS_BOOT \
  --cap-drop SYS_MODULE \
  --cap-drop SYS_NICE \
  --cap-drop SYS_PACCT \
  --cap-drop SYS_PTRACE \
  --cap-drop SYS_RAWIO \
  --cap-drop SYS_RESOURCE \
  --cap-drop SYS_TIME \
  --cap-drop SYS_TTY_CONFIG \
  --cap-drop WAKE_ALARM \
  homeassistant/home-assistant:latest >/dev/null

# iobroker
docker run -d \
  --name iobroker \
  --network proxy \
  --restart unless-stopped \
  -v iobrokerdata:/opt/iobroker \
  -e PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
  -e DEBIAN_FRONTEND='teletype' \
  -e LANG='de_DE.UTF-8' \
  -e LANGUAGE='de_DE:de' \
  -e LC_ALL='de_DE.UTF-8' \
  -e SETGID='1000' \
  -e SETUID='1000' \
  -e TZ='Europe/Berlin' \
  -e BUILD='2024-08-09T20:07:56+00:00' \
  --entrypoint '/bin/bash' \
  --cap-add AUDIT_WRITE \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  --cap-add FSETID \
  --cap-add KILL \
  --cap-add MKNOD \
  --cap-add NET_BIND_SERVICE \
  --cap-add NET_RAW \
  --cap-add SETFCAP \
  --cap-add SETGID \
  --cap-add SETPCAP \
  --cap-add SETUID \
  --cap-add SYS_CHROOT \
  --cap-drop AUDIT_CONTROL \
  --cap-drop BLOCK_SUSPEND \
  --cap-drop DAC_READ_SEARCH \
  --cap-drop IPC_LOCK \
  --cap-drop IPC_OWNER \
  --cap-drop LEASE \
  --cap-drop LINUX_IMMUTABLE \
  --cap-drop MAC_ADMIN \
  --cap-drop MAC_OVERRIDE \
  --cap-drop NET_ADMIN \
  --cap-drop NET_BROADCAST \
  --cap-drop SYSLOG \
  --cap-drop SYS_ADMIN \
  --cap-drop SYS_BOOT \
  --cap-drop SYS_MODULE \
  --cap-drop SYS_NICE \
  --cap-drop SYS_PACCT \
  --cap-drop SYS_PTRACE \
  --cap-drop SYS_RAWIO \
  --cap-drop SYS_RESOURCE \
  --cap-drop SYS_TIME \
  --cap-drop SYS_TTY_CONFIG \
  --cap-drop WAKE_ALARM \
  buanet/iobroker:latest \
  -c '/opt/scripts/iobroker_startup.sh' >/dev/null

# wg-easy
docker run -d \
  --name wg-easy \
  --network proxy \
  --restart unless-stopped \
  -v /root/.wg-easy:/etc/wireguard \
  -p 51820:51820/udp \
  -e LANG='de' \
  -e WG_HOST='krfer.de' \
  -e PASSWORD_HASH='$2a$12$8I77eHDpjAe3/nxc.UEI3.df1oZXFfXFOgHL4Sq4Zr1NSka/4kJSi' \
  -e PORT='51821' \
  -e WG_PORT='51820' \
  -e PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
  -e NODE_VERSION='20.17.0' \
  -e YARN_VERSION='1.22.22' \
  -e DEBUG='Server,WireGuard' \
  -e WG_DEFAULT_DNS='172.18.0.1' \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --sysctl net.ipv4.ip_forward=1 \
  --cap-add NET_ADMIN \
  --cap-add SYS_MODULE \
  --cap-add AUDIT_WRITE \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  --cap-add FSETID \
  --cap-add KILL \
  --cap-add MKNOD \
  --cap-add NET_BIND_SERVICE \
  --cap-add NET_RAW \
  --cap-add SETFCAP \
  --cap-add SETGID \
  --cap-add SETPCAP \
  --cap-add SETUID \
  --cap-add SYS_CHROOT \
  --cap-drop AUDIT_CONTROL \
  --cap-drop BLOCK_SUSPEND \
  --cap-drop DAC_READ_SEARCH \
  --cap-drop IPC_LOCK \
  --cap-drop IPC_OWNER \
  --cap-drop LEASE \
  --cap-drop LINUX_IMMUTABLE \
  --cap-drop MAC_ADMIN \
  --cap-drop MAC_OVERRIDE \
  --cap-drop NET_BROADCAST \
  --cap-drop SYSLOG \
  --cap-drop SYS_ADMIN \
  --cap-drop SYS_BOOT \
  --cap-drop SYS_NICE \
  --cap-drop SYS_PACCT \
  --cap-drop SYS_PTRACE \
  --cap-drop SYS_RAWIO \
  --cap-drop SYS_RESOURCE \
  --cap-drop SYS_TIME \
  --cap-drop SYS_TTY_CONFIG \
  --cap-drop WAKE_ALARM \
  ghcr.io/wg-easy/wg-easy:latest >/dev/null

# vsftpd
docker run -d \
  --name vsftpd \
  --network proxy \
  --restart always \
  -v /srv/paperlessftp:/home/vsftpd \
  -v a92b37d2597346fcc0ffd23b2d8509b61a5b3f782ad1cabc179a6ed9fd2f9ffd:/var/log/vsftpd \
  -e PASV_ADDRESS='127.0.0.1' \
  -e PASV_MIN_PORT='21100' \
  -e PASV_MAX_PORT='21110' \
  -e FTP_USER='paperless' \
  -e FTP_PASS='Ea4WE7tnZMZh3jF' \
  -e PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
  -e PASV_ADDR_RESOLVE='NO' \
  -e PASV_ENABLE='YES' \
  -e XFERLOG_STD_FORMAT='NO' \
  -e LOG_STDOUT='**Boolean**' \
  -e FILE_OPEN_MODE='0666' \
  -e LOCAL_UMASK='000' \
  -e REVERSE_LOOKUP_ENABLE='YES' \
  -e PASV_PROMISCUOUS='NO' \
  -e PORT_PROMISCUOUS='NO' \
  --cap-add AUDIT_WRITE \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  --cap-add FSETID \
  --cap-add KILL \
  --cap-add MKNOD \
  --cap-add NET_BIND_SERVICE \
  --cap-add NET_RAW \
  --cap-add SETFCAP \
  --cap-add SETGID \
  --cap-add SETPCAP \
  --cap-add SETUID \
  --cap-add SYS_CHROOT \
  --cap-drop AUDIT_CONTROL \
  --cap-drop BLOCK_SUSPEND \
  --cap-drop DAC_READ_SEARCH \
  --cap-drop IPC_LOCK \
  --cap-drop IPC_OWNER \
  --cap-drop LEASE \
  --cap-drop LINUX_IMMUTABLE \
  --cap-drop MAC_ADMIN \
  --cap-drop MAC_OVERRIDE \
  --cap-drop NET_ADMIN \
  --cap-drop NET_BROADCAST \
  --cap-drop SYSLOG \
  --cap-drop SYS_ADMIN \
  --cap-drop SYS_BOOT \
  --cap-drop SYS_MODULE \
  --cap-drop SYS_NICE \
  --cap-drop SYS_PACCT \
  --cap-drop SYS_PTRACE \
  --cap-drop SYS_RAWIO \
  --cap-drop SYS_RESOURCE \
  --cap-drop SYS_TIME \
  --cap-drop SYS_TTY_CONFIG \
  --cap-drop WAKE_ALARM \
  dotkevinwong/vsftpd-arm:latest >/dev/null

log "Starting compose-managed stacks"
compose_up /srv/portainer
compose_up /srv/nginxmanager
compose_up /srv/paperless

log "Restore complete. Current container status:"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
REMOTE

echo "[local] Restore execution finished"
