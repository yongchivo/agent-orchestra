#!/usr/bin/env bash
# Manual verification of the control UI's security controls against a LIVE server.
#
#   scripts/smoke_security.sh <port> [state-dir]
#
# Reads the token from <state-dir>/token (default ~/.uno-orchestra/token), runs one
# request per control, prints PASS/FAIL per check and exits non-zero if any check fails.
set -u

PORT="${1:?usage: $0 <port> [state-dir]}"
STATE_DIR="${2:-$HOME/.uno-orchestra}"
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
check "GET with forged Origin -> 403"               403 "$(code -H "X-Orchestra-Token: $TOKEN" -H "Origin: http://evil.example" "$BASE/api/status")"
check "HEAD with forged Origin -> 403"              403 "$(code -I -H "X-Orchestra-Token: $TOKEN" -H "Origin: http://evil.example" "$BASE/api/status")"
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
check "i18n dictionary is served -> 200"            200 "$(code "$BASE/i18n/es.json")"
check "unlisted i18n file -> 404"                   404 "$(code "$BASE/i18n/fr.json")"
check "brand file outside the allowlist -> 404"     404 "$(code "$BASE/brand/uno-mark-1024.png")"
check "brand path traversal -> 404"                 404 "$(code --path-as-is "$BASE/brand/../token")"
check "brand route is PNG or 404"                   ok  "$(c="$(code "$BASE/brand/favicon.png")"; if [ "$c" = 404 ]; then echo ok; else curl -s -D - -o /dev/null "$BASE/brand/favicon.png" | grep -qi '^Content-Type: image/png' && echo ok || echo "not png ($c)"; fi)"

# GET/HEAD must never modify state: fingerprint the state dir, sweep every endpoint, compare.
fingerprint() { find "$STATE_DIR" -type f -print0 | sort -z | xargs -0 ls -l 2>/dev/null | md5 2>/dev/null || find "$STATE_DIR" -type f -print0 | sort -z | xargs -0 ls -l | md5sum; }
BEFORE="$(fingerprint)"
for m in GET HEAD; do
  for p in /api/start /api/stop /api/branch /api/check "/api/start?repo=/tmp/x" "/api/branch?repo=/tmp/x&name=evil" \
           /api/chat "/api/chat?repo=/tmp/x&message=hi" /api/pipeline/approve /api/pipeline/merge /api/roles/delete "/api/roles/commit?repo=/tmp/x" \
           "/api/roles/rename?repo=/tmp/x&name=a&new_name=b" "/api/roles/from-template?repo=/tmp/x&template=developer" "/api/role-templates/delete?name=developer"; do
    if [ "$m" = HEAD ]; then c="$(code -I -H "X-Orchestra-Token: $TOKEN" "$BASE$p")"; else c="$(code -H "X-Orchestra-Token: $TOKEN" "$BASE$p")"; fi
    check "$m $p is not an action -> 404" 404 "$c"
  done
  for p in / /app.js /style.css /i18n/es.json /i18n/en.json /brand/lockup.png /brand/mark.png /brand/favicon.png /brand/apple-touch.png \
           /api/status /api/results /api/templates /api/role-templates "/api/events?from=0" /api/pipeline "/api/roles?repo=/tmp/x"; do
    if [ "$m" = HEAD ]; then code -I -H "X-Orchestra-Token: $TOKEN" "$BASE$p" > /dev/null; else curl -s -m 3 -H "X-Orchestra-Token: $TOKEN" "$BASE$p" > /dev/null; fi
  done
done
check "state dir unchanged after GET/HEAD sweep"    "$BEFORE" "$(fingerprint)"
check "token file is 0600"                          600 "$(stat -f '%Lp' "$STATE_DIR/token" 2>/dev/null || stat -c '%a' "$STATE_DIR/token")"

echo
if [ "$FAILS" -eq 0 ]; then echo "all checks passed"; else echo "$FAILS check(s) FAILED"; fi
exit $((FAILS > 0))
