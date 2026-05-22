#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="${1:-root@krfer.de}"
LOCAL_RESTORE_SCRIPT="/Users/thomaskoerfer/codex_projects/homeserver/restore_homeserver.sh"

if [ ! -f "$LOCAL_RESTORE_SCRIPT" ]; then
  echo "Missing local restore script: $LOCAL_RESTORE_SCRIPT" >&2
  exit 1
fi

printf '[local] Publishing restore bundle to unencrypted WebDAV via %s\n' "$TARGET_HOST"

cat "$LOCAL_RESTORE_SCRIPT" | ssh -o BatchMode=yes "$TARGET_HOST" 'cat > /root/restore_homeserver.sh && chmod 700 /root/restore_homeserver.sh'

ssh -o BatchMode=yes "$TARGET_HOST" 'bash -s' <<'REMOTE'
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

WEBDAV_VENDOR="${WEBDAV_VENDOR:-other}"
REMOTE_PLAIN_PATH="${REMOTE_PLAIN_PATH:-docker-restore-bundle}"
HOST="$(hostname -s)"
TS="$(date +%F_%H-%M-%S)"

WORK_DIR="$(mktemp -d /tmp/restore-bundle-XXXXXX)"
BUNDLE_DIR="${WORK_DIR}/bundle"
RCLONE_CFG="${WORK_DIR}/rclone.conf"
mkdir -p "$BUNDLE_DIR"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

cp /root/restore_homeserver.sh "${BUNDLE_DIR}/restore_homeserver.sh"
chmod 700 "${BUNDLE_DIR}/restore_homeserver.sh"

cp "$ENV_FILE" "${BUNDLE_DIR}/docker-volume-webdav-backup.env.example"
sed -i 's|^WEBDAV_PASSWORD=.*$|WEBDAV_PASSWORD=""|g' "${BUNDLE_DIR}/docker-volume-webdav-backup.env.example"
sed -i 's|^RCLONE_CRYPT_PASSWORD=.*$|RCLONE_CRYPT_PASSWORD=""|g' "${BUNDLE_DIR}/docker-volume-webdav-backup.env.example"
sed -i 's|^RCLONE_CRYPT_PASSWORD2=.*$|RCLONE_CRYPT_PASSWORD2=""|g' "${BUNDLE_DIR}/docker-volume-webdav-backup.env.example"

cat > "${BUNDLE_DIR}/RESTORE_INSTRUCTIONS.md" <<'DOC'
# Restore Instructions (Homeserver)

## 1) Voraussetzungen auf frischem Server
- Ubuntu/Debian mit SSH-Zugang als root
- Docker + Docker Compose Plugin installiert
- Datei `/etc/docker-volume-webdav-backup.env` vorhanden
- In der Env-Datei gesetzt: `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASSWORD`, `RCLONE_CRYPT_PASSWORD` (optional `RCLONE_CRYPT_PASSWORD2`)

## 2) Bundle-Inhalt
- `restore_homeserver.sh`: stellt Volumes, `/srv`, optional `/root/.wg-easy` und Container wieder her
- `docker-volume-webdav-backup.env.example`: Vorlage ohne Passwörter
- Netzwerkdetails liegen nur im verschlüsselten Backup (`docker-network-report.txt`, `docker-network-inspect.json`)

## 3) Restore ausführen
- Script auf dem Zielserver ausführbar machen: `chmod +x restore_homeserver.sh`
- Standard (letztes Backup): `./restore_homeserver.sh root@<zielserver>`
- Bestimmtes Archiv: `./restore_homeserver.sh root@<zielserver> docker-volumes-<host>-<timestamp>.tar.zst`

## 4) Nachkontrolle
- `docker ps`
- Webzugriff auf nginxmanager/paperless/portainer
- WireGuard und FTP-Pfad prüfen
DOC

cat > "$RCLONE_CFG" <<RC
[webdavplain]
type = webdav
url = ${WEBDAV_URL}
vendor = ${WEBDAV_VENDOR}
user = ${WEBDAV_USER}
pass = $(rclone obscure "$WEBDAV_PASSWORD")
RC
chmod 600 "$RCLONE_CFG"

TARGET="webdavplain:${REMOTE_PLAIN_PATH}/${HOST}"
echo "Uploading restore bundle to ${TARGET}"
rclone --config "$RCLONE_CFG" copy "$BUNDLE_DIR" "$TARGET" --checksum --transfers 2 --checkers 4

echo "Bundle files uploaded:"
rclone --config "$RCLONE_CFG" lsf "$TARGET" | sed -n '1,40p'
REMOTE
