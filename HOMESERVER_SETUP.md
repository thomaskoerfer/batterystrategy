# Homeserver Setup (Agent Handover)

## 1) Purpose
This document is the operational handover for the homeserver behind `root@krfer.de` (`docker-arm`).
It is written so another agent can connect, understand the system quickly, and operate safely.

Scope is general server operations. Backup/restore is referenced only where it affects day-to-day handling.

## 2) Access and Environment
- SSH target: `root@krfer.de`
- Hostname: `docker-arm`
- OS: Debian 12 (bookworm), ARM64 VM (Hetzner)
- Docker: `20.10.24`
- Compose runtime used on host: `docker-compose` v1 (`1.29.2`)
- Disk (latest check): `/` around 86% used (watch free space closely)

Quick connect check:
```bash
ssh root@krfer.de 'hostname && docker ps --format "table {{.Names}}\t{{.Status}}"'
```

## 3) High-Level Architecture
The server is Docker-centric:
- Core reverse proxy network: `proxy` (bridge)
- Mix of:
1. Compose-managed stacks under `/srv`
2. Standalone `docker run` containers (managed by scripts)

### 3.1 Compose stacks (`/srv`)
- `/srv/nginxmanager/docker-compose.yml`
- `/srv/portainer/docker-compose.yml`
- `/srv/paperless/docker-compose.yml`

All three use external network `proxy`.

### 3.2 Standalone containers
- `home-assistant`
- `iobroker`
- `wg-easy`
- `vsftpd`

These are not currently defined in compose files in `/srv`; they are recreated through maintenance scripts.

## 4) Live Service Inventory (Current)
### 4.1 Containers
- `nginxmanager` (`jc21/nginx-proxy-manager:latest`)
- `portainer` (`portainer/portainer-ce`)
- `paperless_webserver_1` + `paperless_broker_1`
- `home-assistant`
- `iobroker`
- `wg-easy`
- `vsftpd`

### 4.2 Docker networks
- `proxy` (shared app network)
- `paperless_default`
- default Docker networks: `bridge`, `host`, `none`

### 4.3 Docker volumes
- `homeassistant_config`
- `iobrokerdata`
- `paperless_data`
- `paperless_media`
- `paperless_redisdata`
- `a92b37d...` (vsftpd logs volume)

## 5) Filesystem Layout
### 5.1 Server-side
- `/srv/nginxmanager` (compose + app data)
- `/srv/portainer` (compose + data)
- `/srv/paperless` (compose + export etc.)
- `/srv/paperlessftp` (FTP landing area, mapped into Paperless consume path)
- `/root/.wg-easy` (wg-easy bind-mounted config/state)

### 5.2 Local repo (this workspace)
Main operational scripts:
- `update_homeserver.sh`
- `setup_webdav_volume_sync.sh`
- `restore_homeserver.sh`
- `publish_restore_bundle.sh`

Other files (energy strategy tooling) in this repo are not the core server orchestration path.

## 6) Operational Scripts and Responsibilities
### 6.1 `update_homeserver.sh`
Purpose:
- SSH into server
- apt update/upgrade/autoremove
- manage container image updates
- recreate standalone containers with defined run arguments
- restart compose stacks in `/srv/*`

Usage:
```bash
./update_homeserver.sh
# or
./update_homeserver.sh root@krfer.de
```

### 6.2 `setup_webdav_volume_sync.sh`
Purpose:
- Installs/updates backup service/timer and backup script on server
- Writes/extends `/etc/docker-volume-webdav-backup.env`
- Deploys `/usr/local/sbin/docker-volume-webdav-backup`

Note:
- Current timer target is weekly (`Sun 03:30 UTC` + randomized delay).

### 6.3 `restore_homeserver.sh`
Purpose:
- Pulls chosen backup from WebDAV (through crypt remote)
- Restores volumes + `/srv` (+ optional `/root/.wg-easy` when present)
- Recreates standalone containers and brings compose stacks up

Important:
- This is a destructive restore path (stops/removes running containers first).

### 6.4 `publish_restore_bundle.sh`
Purpose:
- Publishes non-secret restore package to unencrypted WebDAV area:
1. restore script
2. env example (passwords blanked)
3. restore instructions

## 7) Paperless / FTP Integration Notes
This is operationally important:
- `paperless` consumes from bind mount `/srv/paperlessftp/paperless`
- `vsftpd` exposes `/srv/paperlessftp` as FTP root
- Ownership/permissions on these paths are intentionally tuned for cross-container write access

When touching these paths, verify after changes:
```bash
ssh root@krfer.de 'stat -c "%n %u:%g %A" /srv/paperlessftp /srv/paperlessftp/paperless'
```

## 8) Standard Agent Workflow (Recommended)
1. Connect and inspect health:
```bash
ssh root@krfer.de 'docker ps --format "table {{.Names}}\t{{.Status}}"; df -h /'
```
2. Confirm compose files under `/srv` before any stack operations.
3. For updates, prefer `update_homeserver.sh` over ad-hoc container recreation.
4. Keep `proxy` network intact; many services depend on it.
5. Avoid exposing secrets in logs, commits, or generated docs.

## 9) Known Risks / Gotchas
- Low free disk can break maintenance/backup jobs.
- Compose runtime on host is `docker-compose` v1; do not assume plugin-only behavior.
- Standalone containers are defined by scripts, not compose files, so state drift is possible if edited manually on host.
- Paths and permissions around `/srv/paperlessftp` are critical for ingestion.

## 10) Quick Troubleshooting Commands
Container state:
```bash
ssh root@krfer.de 'docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"'
```

Networks:
```bash
ssh root@krfer.de 'docker network ls && docker network inspect proxy >/dev/null && echo proxy_ok'
```

Compose stack sanity:
```bash
ssh root@krfer.de 'cd /srv/paperless && docker-compose config --services'
ssh root@krfer.de 'cd /srv/nginxmanager && docker-compose config --services'
ssh root@krfer.de 'cd /srv/portainer && docker-compose config --services'
```

Disk pressure:
```bash
ssh root@krfer.de 'df -h /; du -sh /var/lib/docker 2>/dev/null; du -sh /srv 2>/dev/null'
```

## 11) Do/Don’t for Follow-up Agents
Do:
- Treat `/srv` and Docker objects as source of truth for running services.
- Use scripted paths first.
- Document every structural change in this file.

Don’t:
- Rotate or rewrite secrets in docs.
- Remove `proxy` network unless you recreate and reattach services intentionally.
- Assume backup/restore scripts are safe for dry-run on production without explicit approval.
