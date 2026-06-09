export LIMRA_CORS_ALLOW_ORIGINS="${LIMRA_CORS_ALLOW_ORIGINS:-http://127.0.0.1:5173,http://localhost:5173}"
PORT="${PORT:-8080}"
uvicorn limra_native:app --port "$PORT" --host 0.0.0.0 --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" --reload
