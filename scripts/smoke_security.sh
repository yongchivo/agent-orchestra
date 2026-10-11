#!/usr/bin/env bash
# Manual verification of the control UI's security controls against a LIVE server.
#
#   scripts/smoke_security.sh <port> [state-dir]
#
# Reads the token from <state-dir>/token (default ~/.agent-orchestra/token), runs one
# request per control, prints PASS/FAIL per check and exits non-zero if any check fails.
set -u

PORT="${1:?usage: $0 <port> [state-dir]}"
STATE_DIR="${2:-$HOME/.agent-orchestra}"
TOKEN="$(cat "$STATE_DIR/token")" || { echo "cannot read $STATE_DIR/token"; exit 2; }
BASE="http://127.0.0.1:$PORT"
HOST="127.0.0.1:$PORT"
ORIGIN="http://127.0.0.1:$PORT"
FAILS=0

check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then printf 'PASS  %-58s %s\n' "$1" "$3"
  else printf 'FAIL  %-58s expected %s, got %s\n' "$1" "$2" "$3"; FAILS=$((FAILS + 1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
headers_present() {  # headers_present <curl args...> -> "ok" or the missing header names
  local out missing=""
  out="$(curl -s -D - -o /dev/null "$@")"
  for h in Content-Security-Policy X-Frame-Options Referrer-Policy X-Content-Type-Options Cache-Control; do
    printf '%s' "$out" | grep -qi "^$h:" || missing="$missing $h"
  done
  [ -z "$missing" ] && echo ok || echo "missing:$missing"
}

check "GET /api/status without token -> 401"        401 "$(code "$BASE/api/status")"
check "GET /api/status with token -> 200"           200 "$(code -H "X-Orchestra-Token: $TOKEN" "$BASE/api/status")"
check "token in query string is ignored -> 401"     401 "$(code "$BASE/api/status?token=$TOKEN")"
check "GET /api/events without token -> 401"        401 "$(code "$BASE/api/events?from=0")"
check "POST with forged Origin -> 403"              403 "$(code -X POST -H "X-Orchestra-Token: $TOKEN" -H "Origin: http://evil.example" -H "Content-Type: application/json" -d '{}' "$BASE/api/check")"
check "POST with correct Origin -> 200"             200 "$(code -X POST -H "X-Orchestra-Token: $TOKEN" -H "Origin: $ORIGIN" -H "Content-Type: application/json" -d '{}' "$BASE/api/check")"
check "GET with forged Host -> 403"                 403 "$(code -H "Host: evil.example" "$BASE/")"
check "POST non-JSON Content-Type -> 415"           415 "$(code -X POST -H "X-Orchestra-Token: $TOKEN" -H "Origin: $ORIGIN" -H "Content-Type: text/plain" -d '{}' "$BASE/api/check")"
check "HEAD / -> 200"                               200 "$(code -I "$BASE/")"
check "HEAD /api/status with token -> 200"          200 "$(code -I -H "X-Orchestra-Token: $TOKEN" "$BASE/api/status")"
check "HEAD / has no body"                          0   "$(curl -s -I "$BASE/" | grep -ci '^<!doctype')"
check "security headers on GET /"                   ok  "$(headers_present "$BASE/")"
check "security headers on GET /api/status"         ok  "$(headers_present -H "X-Orchestra-Token: $TOKEN" "$BASE/api/status")"
check "security headers on 401 error"               ok  "$(headers_present "$BASE/api/status")"
check "security headers on 403 error"               ok  "$(headers_present -H "Host: evil.example" "$BASE/")"
check "security headers on 404 error"               ok  "$(headers_present "$BASE/nope")"
check "security headers on 501 error (PUT)"         ok  "$(headers_present -X PUT "$BASE/api/status")"
check "CSP forbids inline script"                   1   "$(curl -s -D - -o /dev/null "$BASE/" | grep -ci "script-src 'self'")"
check "unlisted file is not served -> 404"          404 "$(code "$BASE/server.py")"
check "token file is 0600"                          600 "$(stat -f '%Lp' "$STATE_DIR/token" 2>/dev/null || stat -c '%a' "$STATE_DIR/token")"

echo
if [ "$FAILS" -eq 0 ]; then echo "all checks passed"; else echo "$FAILS check(s) FAILED"; fi
exit $((FAILS > 0))
