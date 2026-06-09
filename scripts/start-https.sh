#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${LIMIRA_RUNTIME_DIR:-"$ROOT_DIR/limira-runtime"}"
CADDY_DIR="$RUNTIME_DIR/caddy"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"
CADDY_BIN="${LIMIRA_CADDY_BIN:-"$CADDY_DIR/caddy"}"
CADDYFILE="${LIMIRA_CADDYFILE:-"$ROOT_DIR/deploy/caddy/Caddyfile"}"

LIMIRA_DOMAIN="${LIMIRA_DOMAIN:-limira-inc.com}"
LIMIRA_FRONTEND_UPSTREAM="${LIMIRA_FRONTEND_UPSTREAM:-127.0.0.1:5173}"
CADDY_ACME_EMAIL="${CADDY_ACME_EMAIL:-admin@$LIMIRA_DOMAIN}"

mkdir -p "$CADDY_DIR" "$LOG_DIR" "$PID_DIR"

public_ip() {
	curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true
}

domain_ipv4_records() {
	if command -v dig >/dev/null 2>&1; then
		{
			dig +short A "$LIMIRA_DOMAIN" 2>/dev/null
			dig @1.1.1.1 +short A "$LIMIRA_DOMAIN" 2>/dev/null
			dig @8.8.8.8 +short A "$LIMIRA_DOMAIN" 2>/dev/null
		} | awk 'NF' | sort -u
	else
		getent ahostsv4 "$LIMIRA_DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u
	fi
}

download_caddy() {
	if [[ -x "$CADDY_BIN" ]]; then
		return
	fi
	echo "Downloading Caddy to $CADDY_BIN"
	local tmp_file
	tmp_file="$(mktemp)"
	curl -fL --max-time 120 \
		"https://caddyserver.com/api/download?os=linux&arch=amd64" \
		-o "$tmp_file"
	install -m 0755 "$tmp_file" "$CADDY_BIN"
	rm -f "$tmp_file"
}

can_bind_low_ports() {
	if command -v getcap >/dev/null 2>&1 && getcap "$CADDY_BIN" 2>/dev/null | grep -q "cap_net_bind_service=ep"; then
		return 0
	fi
	python3 - <<'PY'
import socket
for port in (80, 443):
    sock = socket.socket()
    try:
        sock.bind(("0.0.0.0", port))
    except PermissionError:
        raise SystemExit(1)
    except OSError:
        pass
    finally:
        sock.close()
PY
}

check_dns() {
	local current_ip records
	current_ip="$(public_ip)"
	records="$(domain_ipv4_records | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
	if [[ -z "$current_ip" ]]; then
		echo "Could not determine this server's public IPv4 address." >&2
		return 1
	fi
	if [[ -z "$records" ]]; then
		cat >&2 <<EOF
$LIMIRA_DOMAIN has no public A record yet.
Add this DNS record before starting HTTPS:
  Type: A
  Host: @
  Value: $current_ip
EOF
		return 1
	fi
	if ! printf '%s\n' $records | grep -Fxq "$current_ip"; then
		cat >&2 <<EOF
$LIMIRA_DOMAIN does not point to this server yet.
Current server IPv4: $current_ip
Current domain A records: $records
Set the domain A record to $current_ip, then rerun this script.
EOF
		return 1
	fi
}

kill_caddy() {
	local pid_file="$PID_DIR/caddy.pid"
	if [[ ! -f "$pid_file" ]]; then
		return
	fi
	local pid
	pid="$(cat "$pid_file" 2>/dev/null || true)"
	rm -f "$pid_file"
	if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
		echo "Stopping Caddy pid $pid"
		kill "$pid" 2>/dev/null || true
		for _ in $(seq 1 30); do
			if ! kill -0 "$pid" 2>/dev/null; then
				return
			fi
			sleep 0.2
		done
		kill -9 "$pid" 2>/dev/null || true
	fi
}

port_pids() {
	local port="$1"
	if command -v lsof >/dev/null 2>&1; then
		lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
	elif command -v fuser >/dev/null 2>&1; then
		fuser "$port/tcp" 2>/dev/null || true
	fi
}

stop_ports() {
	local pids
	pids="$(port_pids 80; port_pids 443)"
	pids="$(printf '%s\n' $pids | awk 'NF' | sort -u)"
	for pid in $pids; do
		if [[ "$pid" =~ ^[0-9]+$ && "$pid" != "$$" && "$pid" != "1" ]]; then
			echo "Stopping process on 80/443 pid $pid"
			kill "$pid" 2>/dev/null || true
		fi
	done
}

start_caddy() {
	download_caddy
	local failed=0
	if ! check_dns; then
		failed=1
	fi
	if ! can_bind_low_ports; then
		cat >&2 <<EOF
Current user cannot bind ports 80/443.
Run this once, then rerun ./scripts/start-https.sh:
  sudo setcap cap_net_bind_service=+ep "$CADDY_BIN"
EOF
		failed=1
	fi
	if [[ "$failed" -ne 0 ]]; then
		exit 1
	fi

	kill_caddy
	stop_ports
	export LIMIRA_DOMAIN LIMIRA_FRONTEND_UPSTREAM CADDY_ACME_EMAIL
	export XDG_DATA_HOME="$CADDY_DIR/data"
	export XDG_CONFIG_HOME="$CADDY_DIR/config"
	mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME"

	local log_file="$LOG_DIR/caddy.log"
	setsid "$CADDY_BIN" run --config "$CADDYFILE" --adapter caddyfile \
		>>"$log_file" 2>&1 < /dev/null &
	echo $! > "$PID_DIR/caddy.pid"
	echo "Started Caddy for https://$LIMIRA_DOMAIN (pid $(cat "$PID_DIR/caddy.pid"), log $log_file)"
}

status_caddy() {
	local pid=""
	if [[ -f "$PID_DIR/caddy.pid" ]]; then
		pid="$(cat "$PID_DIR/caddy.pid" 2>/dev/null || true)"
	fi
	if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
		echo "caddy: running pid $pid"
	else
		echo "caddy: stopped"
	fi
}

case "${1:-restart}" in
	start|restart)
		start_caddy
		;;
	stop)
		kill_caddy
		;;
	status)
		status_caddy
		;;
	*)
		echo "Usage: $0 [start|restart|stop|status]" >&2
		exit 2
		;;
esac
