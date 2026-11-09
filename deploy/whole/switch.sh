#!/bin/bash
set -euo pipefail
umask 077

release=/opt/root-demo/whole/current
overlay="$release/deploy/whole"
config=/etc/caddy/Caddyfile
legacy=/var/lib/root-demo
state=/var/lib/root-demo-whole
cutover_guard=/run/root-demo-whole.cutover
whole_env=/etc/root-demo/whole.env
site=site.example.invalid
build=v4-e22b861e0ab5
units=(root-demo-auth-gate root-demo-site root-demo-whole)

test "$(id -u)" -eq 0
exec 9>/opt/root-demo/whole/.deploy.lock
flock -n 9
test ! -e "$state"
test ! -e "$legacy/generation.paused"
test ! -e "$whole_env"
test ! -e "$cutover_guard"
test "$(systemctl is-active root-demo)" = active
test "$(systemctl is-active caddy)" = active
for unit in "${units[@]}"; do
    test ! -e "/etc/systemd/system/$unit.service"
done

install -d -m 0700 /var/backups/root-demo
backup="$(mktemp -d /var/backups/root-demo/pre-whole-f5d11b4.XXXXXXXX)"
cp -p "$config" "$backup/Caddyfile"
cp -p /etc/root-demo/workbench.env "$backup/workbench.env"
readlink /opt/root-demo/current > "$backup/legacy-release"
systemctl is-enabled root-demo > "$backup/legacy-enabled" || true

next="$backup/Caddyfile.next"
python3 -B - "$config" "$overlay/Caddyfile" "$next" <<'PY'
from pathlib import Path
import re
import sys

base, overlay, destination = (Path(arg) for arg in sys.argv[1:])
text = base.read_text()
legacy = list(re.finditer(r"(?m)^101[.]32[.]127[.]12\s+\{", text))
assert len(legacy) == 1
tail = text[legacy[0].start():]
assert "reverse_proxy 127.0.0.1:8773" in tail
assert tail.count("{") == tail.count("}")
assert not re.search(r"(?m)^\S.*\{", tail[tail.index("\n")+1:])
destination.write_text(text[:legacy[0].start()] + overlay.read_text())
PY
caddy validate --config "$next" --adapter caddyfile

proxy_touched=0
services_touched=0
legacy_paused=0
legacy_stopped=0
rollback() {
    local code=$?
    trap - EXIT
    if (( code != 0 )); then
        set +e
        local rollback_failed=0
        if (( proxy_touched )); then
            install -m "$(stat -c %a "$backup/Caddyfile")" "$backup/Caddyfile" "$config" &&
                systemctl reload caddy || rollback_failed=1
        fi
        if (( services_touched )); then
            systemctl stop "${units[@]}" || rollback_failed=1
            systemctl disable "${units[@]}" >/dev/null 2>&1 || rollback_failed=1
        fi
        if (( legacy_paused )); then
            rm -f -- "$legacy/generation.paused" || rollback_failed=1
        fi
        if (( legacy_stopped )); then
            if grep -qx enabled "$backup/legacy-enabled"; then
                systemctl enable root-demo >/dev/null 2>&1 || rollback_failed=1
            fi
            systemctl start root-demo || rollback_failed=1
        fi
        if (( ! rollback_failed )); then
            if [[ -e "$state" ]]; then
                mv -- "$state" "$backup/failed-whole-state" || rollback_failed=1
            fi
            for unit in "${units[@]}"; do
                rm -f -- "/etc/systemd/system/$unit.service" || rollback_failed=1
            done
            rm -f -- "$whole_env" || rollback_failed=1
            systemctl daemon-reload || rollback_failed=1
        fi
        if (( ! rollback_failed )); then
            rm -f -- "$cutover_guard" || rollback_failed=1
        fi
        if (( rollback_failed )); then
            printf 'Rollback incomplete. Check services, proxy and retained data before retrying.\n' >&2
        fi
        printf 'Rollback path: %s\n' "$backup" >&2
    fi
    exit "$code"
}
trap rollback EXIT
trap 'exit 1' TERM INT

install -m 0644 /dev/null "$cutover_guard"
install -m 0600 "$overlay/whole.env.example" "$whole_env"
for unit in "${units[@]}"; do
    install -m 0644 "$overlay/$unit.service" "/etc/systemd/system/$unit.service"
done
systemd-analyze verify "${units[@]/%/.service}"
systemctl daemon-reload

( set -o noclobber; : > "$legacy/generation.paused" )
legacy_paused=1
sleep 2
for attempt in {1..12}; do
    busy=''
    for run in "$legacy"/run-*; do
        if [[ -d "$run" && ! -f "$run/generation.json" ]] &&
            [[ -n "$(find "$run" -maxdepth 0 -mmin -3 -print -quit)" ]]; then
            busy=1
            break
        fi
    done
    if [[ -z "$busy" ]]; then break; fi
    sleep 2
done
if [[ -n "$busy" ]]; then
    echo 'Legacy request still running. Retry after it finishes.' >&2
    exit 1
fi
legacy_stopped=1
systemctl stop root-demo
tar -C /var/lib -czf "$backup/legacy-data.tar.gz" root-demo
cp -a "$legacy" "$state"
rm -f -- "$legacy/generation.paused"
(cd "$release"; sudo -u root-demo env PYTHONPATH="$release" python3 -B "$overlay/migrate_legacy.py" "$state")
services_touched=1
systemctl start "${units[@]}"

curl --fail --silent --show-error --retry 4 --retry-delay 1 --retry-connrefused --max-time 3 \
    http://127.0.0.1:8774/api/config | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["configured"] and x["build"]==sys.argv[1]' "$build"
proxy_touched=1
install -m "$(stat -c %a "$backup/Caddyfile")" "$next" "$config"
systemctl reload caddy
curl --fail --silent --show-error --max-time 6 --connect-to workbench.example.invalid:443:127.0.0.1:443 \
    https://workbench.example.invalid/api/config | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["configured"] and x["build"]==sys.argv[1]' "$build"

for attempt in {1..20}; do
    if curl --fail --silent --max-time 2 --connect-to "$site:443:127.0.0.1:443" \
        "https://$site/" | grep -F 'https://workbench.example.invalid/' >/dev/null; then
        site_ok=1
        break
    fi
    sleep 1
done
test "${site_ok:-0}" -eq 1
systemctl enable "${units[@]}" >/dev/null
systemctl disable root-demo >/dev/null
rm -f -- "$state/generation.paused"
rm -f -- "$cutover_guard"
trap - EXIT TERM INT
printf 'Active %s\nBackup %s\n' "$build" "$backup"
