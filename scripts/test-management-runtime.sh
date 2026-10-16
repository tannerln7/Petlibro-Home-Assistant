#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image="${IMAGE:-petlibro-local:local}"
direct_port="${MANAGEMENT_SMOKE_DIRECT_PORT:-18098}"
ingress_port="${MANAGEMENT_SMOKE_INGRESS_PORT:-18099}"
container="petlibro-management-smoke-$$"
temporary="$(mktemp -d)"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
  docker run --rm --entrypoint sh -v "${temporary}:/cleanup" "${image}" \
    -c 'rm -rf -- /cleanup/data' >/dev/null 2>&1 || true
  rm -rf -- "${temporary}"
}
trap cleanup EXIT

wait_for_http() {
  local url="$1"
  for _ in $(seq 1 20); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_worker() {
  local url="$1"
  local output="$2"
  for _ in $(seq 1 20); do
    if curl -fsS "${url}" >"${output}" 2>/dev/null &&
      jq -e \
        '.integrations[] | select(.id == "capability-worker" and .availability == "ready")' \
        "${output}" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

run_addon() {
  local direct_access="$1"
  docker run -d --name "${container}" --network host \
    -e "MANAGEMENT_DIRECT_ACCESS=${direct_access}" \
    -e "MANAGEMENT_DIRECT_PORT=${direct_port}" \
    -e MANAGEMENT_DIRECT_ALLOWED_CIDR=127.0.0.1/32 \
    -e "PETLIBRO_INGRESS_PORT=${ingress_port}" \
    -e PETLIBRO_INGRESS_ALLOW=127.0.0.1/32 \
    -e PETLIBRO_STATE_AGENT_URL=http://127.0.0.1:18900 \
    -e PETLIBRO_STATE_AGENT_TOKEN=synthetic-smoke-token \
    -e DEVICE_DISCOVERY=0 \
    -e MQTT_HOST=127.0.0.1 \
    -v "${temporary}/data:/data" \
    "${image}" >/dev/null
}

cd "${repo_root}"
run_addon 1
wait_for_http "http://127.0.0.1:${direct_port}/api/v1/system/status"
wait_for_worker "http://127.0.0.1:${direct_port}/api/v1/system/status" \
  "${temporary}/system-status.json"

curl -fsS "http://127.0.0.1:${direct_port}/" >"${temporary}/direct.html"
curl -fsS "http://127.0.0.1:${direct_port}/operations" >"${temporary}/deep.html"
curl -fsS "http://127.0.0.1:${direct_port}/api/v1/devices/overview" \
  >"${temporary}/device-overview.json"
grep -q '"apiVersion":"v1"' "${temporary}/device-overview.json"
grep -q '"id":"appdaemon"' "${temporary}/device-overview.json"
grep -q '"id":"go2rtc"' "${temporary}/device-overview.json"
grep -q '"id":"mosquitto"' "${temporary}/device-overview.json"
curl -fsS -H 'X-Ingress-Path: /api/hassio_ingress/test-token' \
  "http://127.0.0.1:${ingress_port}/" >"${temporary}/ingress.html"
grep -q '/api/hassio_ingress/test-token/assets/' "${temporary}/ingress.html"
if grep -q '__PETLIBRO_BASE__' "${temporary}/ingress.html"; then
  echo "Ingress response still contains the build-time base sentinel." >&2
  exit 1
fi

operation_json="$(curl -fsS -X POST \
  -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:${direct_port}/api/v1/actions/state-agent-inspection")"
operation_id="$(printf '%s' "${operation_json}" | sed -n \
  's/.*"operationId":"\([^"]*\)".*/\1/p')"
test -n "${operation_id}"

set +e
curl --http1.1 -sS --max-time 2 \
  -D "${temporary}/websocket.headers" -o "${temporary}/websocket.body" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "http://127.0.0.1:${direct_port}/api/v1/operations/${operation_id}/events?after=0" \
  2>/dev/null
websocket_result=$?
set -e
test "${websocket_result}" -eq 28
grep -q '101 Switching Protocols' "${temporary}/websocket.headers"

test "$(docker exec "${container}" stat -c '%a' \
  /run/petlibro-management-http/management.sock)" = 770
test "$(docker exec "${container}" stat -c '%a' \
  /run/petlibro-management/worker/worker.sock)" = 600

docker rm -f "${container}" >/dev/null
run_addon 0
wait_for_http "http://127.0.0.1:${ingress_port}/api/v1/system/status"
curl -fsS "http://127.0.0.1:${ingress_port}/api/v1/devices/overview" \
  >/dev/null
curl -fsS "http://127.0.0.1:${ingress_port}/api/v1/diagnostics/report" \
  >/dev/null
curl -fsS "http://127.0.0.1:${ingress_port}/api/v1/operation-events?limit=20" \
  >/dev/null
curl -fsS "http://127.0.0.1:${ingress_port}/api/v1/operations/${operation_id}" \
  >/dev/null
if curl -fsS --max-time 1 "http://127.0.0.1:${direct_port}/" >/dev/null 2>&1; then
  echo "Direct management listener remained reachable while disabled." >&2
  exit 1
fi

echo "Petlibro Management runtime smoke test passed."
