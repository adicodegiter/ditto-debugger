#!/usr/bin/env bash
# Report the status of every Ditto container and whether the API answers on localhost.
# Exit code 0 = all containers running (and healthy where a healthcheck exists) AND API OK.
set -uo pipefail

usage() {
  cat <<EOF
Usage: deploy/health_check.sh [--wait SECONDS] [--quiet]

Reports each Ditto container's state/health plus API reachability on localhost.

Options:
  --wait SECONDS  Re-check every 5s until everything is healthy or SECONDS elapse
                  (default: 0 = check once)
  --quiet         Only print the final OVERALL line
  -h, --help      Show this help

Environment (read from the shell or the repo-root .env):
  DITTO_BASE_URL default http://127.0.0.1:8080
  DITTO_USER  default ditto
  DITTO_PASS  default ditto
EOF
}

WAIT=0
QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait) WAIT="${2:?--wait needs a number of seconds}"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE=(docker compose --env-file "$SCRIPT_DIR/image-pins.env" -f "$SCRIPT_DIR/ditto/docker-compose.yml")

# Load repo-root .env if present (does not override variables already set).
if [[ -f "$REPO_ROOT/.env" ]]; then
  while IFS='=' read -r k v; do
    k="${k#$'\ufeff'}"
    k="${k%$'\r'}"
    [[ -z "$k" || "$k" =~ ^# ]] && continue
    v="${v%$'\r'}"
    if [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [[ -z "${!k:-}" ]]; then
      export "$k=$v"
    fi
  done < "$REPO_ROOT/.env"
fi
DITTO_BASE_URL="${DITTO_BASE_URL:-http://127.0.0.1:8080}"
DITTO_USER="${DITTO_USER:-ditto}"
DITTO_PASS="${DITTO_PASS:-ditto}"

check_once() {
  local all_ok=0
  local ids
  ids="$("${COMPOSE[@]}" ps -a -q 2>/dev/null)"
  if [[ -z "$ids" ]]; then
    [[ $QUIET -eq 0 ]] && echo "No Ditto containers found (is the stack up? see deploy/reset.sh)"
    return 1
  fi

  [[ $QUIET -eq 0 ]] && printf '%-24s %-10s %-10s %s\n' CONTAINER STATE HEALTH RESULT
  while read -r name state health; do
    health="${health%$'\r'}"
    local result=OK
    if [[ "$state" != "running" ]]; then
      result=FAIL
    elif [[ "$health" != "none" && "$health" != "healthy" ]]; then
      result=FAIL
    fi
    [[ "$result" == FAIL ]] && all_ok=1
    [[ $QUIET -eq 0 ]] && printf '%-24s %-10s %-10s %s\n' "$name" "$state" "$health" "$result"
  done < <(docker inspect --format \
      '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      $ids | sed 's#^/##' | sort)

  # API checks: unauthenticated health endpoint + an authenticated search request.
  local h s
  h="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$DITTO_BASE_URL/health")"
  s="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -u "$DITTO_USER:$DITTO_PASS" \
        "$DITTO_BASE_URL/api/2/search/things?option=size(1)")"
  local api=OK
  [[ "$h" != 200 || "$s" != 200 ]] && api=FAIL && all_ok=1
  [[ $QUIET -eq 0 ]] && echo "API $DITTO_BASE_URL  /health=$h  /api/2/search=$s  -> $api"
  return $all_ok
}

deadline=$(( $(date +%s) + WAIT ))
while true; do
  if check_once; then
    echo "OVERALL: HEALTHY"
    exit 0
  fi
  if (( $(date +%s) >= deadline )); then
    echo "OVERALL: UNHEALTHY"
    exit 1
  fi
  [[ $QUIET -eq 0 ]] && echo "... not healthy yet, retrying in 5s" && echo
  sleep 5
done
