#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="${1:-root@krfer.de}"

echo "[local] Starting update run on ${TARGET_HOST}"

ssh -o BatchMode=yes "${TARGET_HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

warn() {
  printf '\n[%s] WARN: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

run_compose_stack() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    log "SKIP compose stack: $dir (missing)"
    return 0
  fi

  log "Compose stack check in $dir"
  cd "$dir"

  local -a compose_cmd
  if docker compose version >/dev/null 2>&1; then
    compose_cmd=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose_cmd=(docker-compose)
  else
    warn "No compose command found on host; skip stack $dir"
    return 0
  fi

  local needs_restart=0
  local services
  services="$(${compose_cmd[@]} config --services)"

  while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    local cid
    cid="$(${compose_cmd[@]} ps -q "$svc" 2>/dev/null || true)"

    if [ -z "$cid" ]; then
      log "Service $svc not running; stack restart required"
      needs_restart=1
      continue
    fi

    local image_ref running_id new_id
    image_ref="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    running_id="$(docker inspect -f '{{.Image}}' "$cid")"
    if ! docker pull "$image_ref" >/dev/null; then
      warn "Pull failed for $svc image $image_ref; skip restart decision for this service"
      continue
    fi
    new_id="$(docker image inspect -f '{{.Id}}' "$image_ref")"

    if [ "$running_id" != "$new_id" ]; then
      log "Service $svc image changed: $running_id -> $new_id"
      needs_restart=1
    fi
  done <<< "$services"

  if [ "$needs_restart" -eq 1 ]; then
    log "Compose restart in $dir (image change detected)"
    ${compose_cmd[@]} down
    ${compose_cmd[@]} up -d
  else
    log "No image change in $dir; skip down/up"
  fi

  ${compose_cmd[@]} ps
}

recreate_home_assistant() {
  local container="home-assistant"

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "SKIP $container (not found)"
    return 0
  fi

  log "Snapshot + recreate $container"
  local old_image_id
  old_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container old image id: $old_image_id"

  if ! docker pull homeassistant/home-assistant:latest; then
    warn "Pull failed for $container; skip recreate"
    return 0
  fi
  local latest_image_id
  latest_image_id="$(docker image inspect -f '{{.Id}}' homeassistant/home-assistant:latest)"

  if [ "$old_image_id" = "$latest_image_id" ]; then
    log "No image change for $container; skip recreate"
    return 0
  fi

  docker inspect "$container" > /root/home-assistant.inspect.json

  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null

  docker run -d \
    --name "$container" \
    --network proxy \
    --ip 172.18.0.10 \
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

  local new_image_id
  new_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container new image id: $new_image_id"
}

recreate_iobroker() {
  local container="iobroker"

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "SKIP $container (not found)"
    return 0
  fi

  log "Snapshot + recreate $container"
  local old_image_id
  old_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container old image id: $old_image_id"

  if ! docker pull buanet/iobroker:latest; then
    warn "Pull failed for $container; skip recreate"
    return 0
  fi
  local latest_image_id
  latest_image_id="$(docker image inspect -f '{{.Id}}' buanet/iobroker:latest)"

  if [ "$old_image_id" = "$latest_image_id" ]; then
    log "No image change for $container; skip recreate"
    return 0
  fi

  docker inspect "$container" > /root/iobroker.inspect.json

  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null

  docker run -d \
    --name "$container" \
    --network proxy \
    --ip 172.18.0.9 \
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

  local new_image_id
  new_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container new image id: $new_image_id"
}

recreate_wg_easy() {
  local container="wg-easy"

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "SKIP $container (not found)"
    return 0
  fi

  log "Snapshot + recreate $container"
  local old_image_id
  old_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container old image id: $old_image_id"

  if ! docker pull ghcr.io/wg-easy/wg-easy:latest; then
    warn "Pull failed for $container; skip recreate"
    return 0
  fi
  local latest_image_id
  latest_image_id="$(docker image inspect -f '{{.Id}}' ghcr.io/wg-easy/wg-easy:latest)"

  if [ "$old_image_id" = "$latest_image_id" ]; then
    log "No image change for $container; skip recreate"
    return 0
  fi

  docker inspect "$container" > /root/wg-easy.inspect.json

  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null

  docker run -d \
    --name "$container" \
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

  local new_image_id
  new_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container new image id: $new_image_id"
}

recreate_vsftpd() {
  local container="vsftpd"

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "SKIP $container (not found)"
    return 0
  fi

  log "Snapshot + recreate $container"
  local old_image_id
  old_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container old image id: $old_image_id"

  if ! docker pull dotkevinwong/vsftpd-arm:latest; then
    warn "Pull failed for $container; skip recreate"
    return 0
  fi
  local latest_image_id
  latest_image_id="$(docker image inspect -f '{{.Id}}' dotkevinwong/vsftpd-arm:latest)"

  if [ "$old_image_id" = "$latest_image_id" ]; then
    log "No image change for $container; skip recreate"
    return 0
  fi

  docker inspect "$container" > /root/vsftpd.inspect.json

  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null

  docker run -d \
    --name "$container" \
    --network proxy \
    --ip 172.18.0.8 \
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

  local new_image_id
  new_image_id="$(docker inspect -f '{{.Image}}' "$container")"
  log "$container new image id: $new_image_id"
}

log "APT update"
apt-get update -y

log "APT upgrade"
apt-get -y upgrade

log "APT autoremove"
apt-get -y autoremove

recreate_home_assistant
recreate_iobroker
recreate_wg_easy
recreate_vsftpd

run_compose_stack /srv/portainer
run_compose_stack /srv/nginxmanager
run_compose_stack /srv/paperless

log "Prune unused Docker images"
docker image prune -af

log "Final container status"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
REMOTE

echo "[local] Update run completed"
