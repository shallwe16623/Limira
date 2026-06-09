#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${LIMIRA_RUNTIME_DIR:-"$ROOT_DIR/limira-runtime"}"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"

mkdir -p "$LOG_DIR" "$PID_DIR"

load_env_file() {
	local file="$1"
	if [[ -f "$file" ]]; then
		set -a
		# shellcheck disable=SC1090
		. "$file"
		set +a
	fi
}

load_env_file "$ROOT_DIR/.env"
load_env_file "$ROOT_DIR/apps/limira-agent/.env"
load_env_file "$ROOT_DIR/apps/limira-runner/.env"

RUNNER_PORT="${LIMIRA_RUNNER_INTERNAL_PORT:-8091}"
BACKEND_PORT="${LIMIRA_BACKEND_PORT:-8080}"
FRONTEND_PORT="${LIMIRA_STANDALONE_PORT:-5173}"
FRONTEND_HOST="${LIMIRA_STANDALONE_HOST:-0.0.0.0}"

RUNNER_PYTHON="${LIMIRA_RUNNER_PYTHON:-"$ROOT_DIR/apps/limira-runner/.venv/bin/python"}"
BACKEND_PYTHON="${LIMIRA_BACKEND_PYTHON:-"$ROOT_DIR/apps/limira-web/.venv/bin/python"}"
NODE_BIN="${LIMIRA_NODE_BIN:-}"

if [[ ! -x "$RUNNER_PYTHON" ]]; then
	RUNNER_PYTHON="$(command -v python3 || command -v python || true)"
fi
if [[ ! -x "$BACKEND_PYTHON" ]]; then
	BACKEND_PYTHON="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$NODE_BIN" ]]; then
	if command -v node >/dev/null 2>&1; then
		NODE_BIN="$(command -v node)"
	elif [[ -x /tmp/codex-node/node/bin/node ]]; then
		NODE_BIN="/tmp/codex-node/node/bin/node"
	fi
fi

require_executable() {
	local name="$1"
	local path="$2"
	if [[ -z "$path" || ! -x "$path" ]]; then
		echo "Missing executable for $name. Set the matching LIMIRA_* env var." >&2
		exit 1
	fi
}

require_executable "runner Python" "$RUNNER_PYTHON"
require_executable "backend Python" "$BACKEND_PYTHON"
require_executable "Node.js" "$NODE_BIN"

kill_pid_file() {
	local name="$1"
	local pid_file="$PID_DIR/$name.pid"
	if [[ ! -f "$pid_file" ]]; then
		return
	fi
	local pid
	pid="$(cat "$pid_file" 2>/dev/null || true)"
	rm -f "$pid_file"
	if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
		return
	fi
	if ! kill -0 "$pid" 2>/dev/null; then
		return
	fi
	echo "Stopping $name pid $pid"
	kill "$pid" 2>/dev/null || true
	for _ in $(seq 1 30); do
		if ! kill -0 "$pid" 2>/dev/null; then
			return
		fi
		sleep 0.2
	done
	kill -9 "$pid" 2>/dev/null || true
}

port_pids() {
	local port="$1"
	if command -v lsof >/dev/null 2>&1; then
		lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
	elif command -v fuser >/dev/null 2>&1; then
		fuser "$port/tcp" 2>/dev/null || true
	fi
}

kill_port() {
	local port="$1"
	local pids
	pids="$(port_pids "$port" | tr ' ' '\n' | awk 'NF' | sort -u)"
	if [[ -z "$pids" ]]; then
		return
	fi
	echo "Stopping processes listening on port $port: $pids"
	for pid in $pids; do
		if [[ "$pid" =~ ^[0-9]+$ && "$pid" != "$$" && "$pid" != "1" ]]; then
			kill "$pid" 2>/dev/null || true
		fi
	done
	sleep 0.5
	for pid in $pids; do
		if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
			kill -9 "$pid" 2>/dev/null || true
		fi
	done
}

stop_services() {
	kill_pid_file frontend
	kill_pid_file backend
	kill_pid_file runner
	kill_port "$FRONTEND_PORT"
	kill_port "$BACKEND_PORT"
	kill_port "$RUNNER_PORT"
}

start_runner() {
	local log_file="$LOG_DIR/runner.log"
	setsid bash -lc "
		set -euo pipefail
		export RUNNER_TASK_STORE_BACKEND=sqlite
		export RUNNER_ALLOW_SQLITE_TASK_STORE=true
		export RUNNER_SERVICE_TOKEN=\"\${RUNNER_SERVICE_TOKEN:-limira-local-dev-token}\"
		export LIMIRA_RUNNER_INTERNAL_PORT=\"$RUNNER_PORT\"
		cd '$ROOT_DIR/apps/limira-runner'
		exec '$RUNNER_PYTHON' runner_api.py
	" >>"$log_file" 2>&1 < /dev/null &
	echo $! > "$PID_DIR/runner.pid"
	echo "Started runner on :$RUNNER_PORT (pid $(cat "$PID_DIR/runner.pid"), log $log_file)"
}

start_backend() {
	local log_file="$LOG_DIR/backend.log"
	setsid bash -lc "
		set -euo pipefail
		export DATA_DIR=\"\${DATA_DIR:-$RUNTIME_DIR/backend-data}\"
		export LIMIRA_REPOSITORY_BACKEND=sqlite
		export LIMIRA_RUNTIME_STATE_BACKEND=memory
		export LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE=true
		export LIMIRA_OBJECT_STORAGE_BACKEND=filesystem
		export LIMIRA_OBJECT_STORAGE_PATH=\"\${LIMIRA_OBJECT_STORAGE_PATH:-$RUNTIME_DIR/object-storage}\"
		export LIMIRA_OBJECT_BUCKET=\"\${LIMIRA_OBJECT_BUCKET:-limira-local}\"
		export LIMIRA_RUNNER_INTERNAL_URL=\"http://127.0.0.1:$RUNNER_PORT\"
		export LIMIRA_RUNNER_SERVICE_TOKEN=\"\${RUNNER_SERVICE_TOKEN:-limira-local-dev-token}\"
		export LIMIRA_AUTH_SECRET=\"\${LIMIRA_AUTH_SECRET:-limira-local-development-secret}\"
		export LIMIRA_CORS_ALLOW_ORIGINS=\"\${LIMIRA_CORS_ALLOW_ORIGINS:-http://127.0.0.1:$FRONTEND_PORT,http://localhost:$FRONTEND_PORT}\"
		cd '$ROOT_DIR/apps/limira-web/backend'
		exec '$BACKEND_PYTHON' -m uvicorn limira_native:app --host 0.0.0.0 --port '$BACKEND_PORT'
	" >>"$log_file" 2>&1 < /dev/null &
	echo $! > "$PID_DIR/backend.pid"
	echo "Started backend on :$BACKEND_PORT (pid $(cat "$PID_DIR/backend.pid"), log $log_file)"
}

start_frontend() {
	local log_file="$LOG_DIR/frontend.log"
	setsid bash -lc "
		set -euo pipefail
		cd '$ROOT_DIR'
		export LIMIRA_BACKEND_URL=\"\${LIMIRA_BACKEND_URL:-http://127.0.0.1:$BACKEND_PORT}\"
		export LIMIRA_STANDALONE_HOST=\"$FRONTEND_HOST\"
		export LIMIRA_STANDALONE_PORT=\"$FRONTEND_PORT\"
		exec '$NODE_BIN' apps/limira-standalone/server.mjs
	" >>"$log_file" 2>&1 < /dev/null &
	echo $! > "$PID_DIR/frontend.pid"
	echo "Started frontend on :$FRONTEND_PORT (pid $(cat "$PID_DIR/frontend.pid"), log $log_file)"
}

wait_for_url() {
	local name="$1"
	local url="$2"
	for _ in $(seq 1 60); do
		if curl -fsS "$url" >/dev/null 2>&1; then
			echo "$name is ready: $url"
			return
		fi
		sleep 0.5
	done
	echo "$name did not become ready: $url" >&2
	exit 1
}

status_services() {
	for name in runner backend frontend; do
		local pid_file="$PID_DIR/$name.pid"
		local pid=""
		if [[ -f "$pid_file" ]]; then
			pid="$(cat "$pid_file" 2>/dev/null || true)"
		fi
		if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
			echo "$name: running pid $pid"
		else
			echo "$name: stopped"
		fi
	done
}

start_services() {
	stop_services
	start_runner
	start_backend
	start_frontend
	wait_for_url "runner" "http://127.0.0.1:$RUNNER_PORT/health"
	wait_for_url "backend" "http://127.0.0.1:$BACKEND_PORT/health"
	wait_for_url "frontend" "http://127.0.0.1:$FRONTEND_PORT/limira"
	echo
	echo "Limira is running:"
	echo "  Frontend: http://127.0.0.1:$FRONTEND_PORT/limira"
	echo "  Backend:  http://127.0.0.1:$BACKEND_PORT"
	echo "  Runner:   http://127.0.0.1:$RUNNER_PORT"
}

case "${1:-restart}" in
	start|restart)
		start_services
		;;
	stop)
		stop_services
		;;
	status)
		status_services
		;;
	*)
		echo "Usage: $0 [start|restart|stop|status]" >&2
		exit 2
		;;
esac
