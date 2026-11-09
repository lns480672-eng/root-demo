#!/bin/bash
# Upgrade an already active whole deployment without migrating its live data again.
set -euo pipefail
umask 077

old_build=v4-95cc4b425c83
new_build=v4-6e305b8d9a6f
base=/opt/root-demo/whole
current="$base/current"
old="$base/releases/386c053-f35f021"
new="$base/releases/a0f8fc7-journey"
config=/etc/caddy/Caddyfile
guard=/run/root-demo-whole.cutover
state=/var/lib/root-demo-whole
backups=/var/backups/root-demo
service=root-demo-whole

test "$(id -u)" -eq 0
exec 9>"$base/.deploy.lock"
flock -n 9
test "$(readlink "$current")" = "$old"
test -d "$new"
test ! -e "$guard"
test -f "$state/workspace.sqlite3"
for unit in "$service" root-demo-site root-demo-auth-gate caddy; do
    systemctl is-active --quiet "$unit"
done

health() {
    curl --silent --show-error --fail --max-time 4 \
        --connect-to workbench.example.invalid:443:127.0.0.1:443 https://workbench.example.invalid/api/config |
        python3 -c 'import json,sys; assert json.load(sys.stdin)=={"configured":True,"build":sys.argv[1],"staged":True}' "$1"
}

await_build() {
    for attempt in {1..8}; do
        if health "$1"; then return 0; fi
        sleep 1
    done
    return 1
}

health "$old_build"
install -d -m 0700 "$backups"
backup="$(mktemp -d "$backups/pre-a0f8fc7.XXXXXXXX")"
cp -p "$config" "$backup/Caddyfile"
printf '%s\n' "$old" > "$backup/release"
stat -c '%a %u %g' "$config" > "$backup/proxy-mode"
read -r mode owner group < "$backup/proxy-mode"

candidate_config="$backup/Caddyfile.next"
python3 -B - "$config" "$old/deploy/whole/Caddyfile" "$new/deploy/whole/Caddyfile" "$candidate_config" <<'PY'
from pathlib import Path
import sys

live, old, new, dest = map(Path, sys.argv[1:])
current, previous, candidate = (path.read_bytes() for path in (live, old, new))
if not current.endswith(previous) or current.count(previous) != 1:
    raise SystemExit("Live Caddy configuration diverged from the expected release.")
if b"/root-demo-whole.cutover" not in candidate:
    raise SystemExit("Candidate proxy is missing its cutover guard.")
dest.write_bytes(current[:-len(previous)] + candidate)
PY
caddy validate --config "$candidate_config" --adapter caddyfile

set_config() {
    install -o "$owner" -g "$group" -m "$mode" "$1" "$config.upgrade-next" &&
        mv -Tf "$config.upgrade-next" "$config"
}

switch_pointer() {
    ln -s "$1" "$base/.upgrade-next" &&
        mv -Tf "$base/.upgrade-next" "$current"
}

snapshot() {
    python3 -B - "$state/workspace.sqlite3" "$1" <<'PYSQL'
from hashlib import sha256
from pathlib import Path
import sqlite3
import sys

uri = Path(sys.argv[1]).as_uri() + "?mode=ro"
added = {"analysis_rounds", "analysis_draft_offers", "table_restore_drafts"}
with sqlite3.connect(uri, uri=True) as db:
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise SystemExit("Database integrity check failed.")
    tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    if sys.argv[2] == "after":
        if not added <= tables.keys():
            raise SystemExit("Expected additive migration did not run.")
        for table in added:
            if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise SystemExit("New tables should be empty before reopening traffic.")
            del tables[table]
    else:
        for table in added & tables.keys():
            if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise SystemExit("Existing new tables contain data; inspect before retrying.")
            del tables[table]
    digest = sha256()
    for table, schema in tables.items():
        name = table.replace('"', '""')
        digest.update((schema + "\n").encode())
        for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            digest.update((repr(row) + "\n").encode())
print(digest.hexdigest())
PYSQL
}

proxy_changed=0
service_changed=0
guard_owned=0
finish() {
    status=$?
    trap - EXIT
    set +e
    if (( status != 0 )); then
        rollback_ok=1
        if (( service_changed )); then
            systemctl stop "$service" || rollback_ok=0
            if [[ "$(readlink "$current")" != "$old" ]]; then
                switch_pointer "$old" || rollback_ok=0
            fi
            systemctl start "$service" && await_build "$old_build" || rollback_ok=0
        fi
        if (( proxy_changed && rollback_ok )); then
            set_config "$backup/Caddyfile" && systemctl reload caddy &&
                await_build "$old_build" || rollback_ok=0
        fi
        if (( guard_owned && rollback_ok )); then rm -f -- "$guard"; fi
        if (( rollback_ok )); then
            printf 'Upgrade aborted; original build %s remains available. Backup: %s\n' "$old_build" "$backup" >&2
        else
            printf 'Rollback incomplete; retained state and guard require inspection. Backup: %s\n' "$backup" >&2
        fi
    fi
    rm -f -- "$base/.upgrade-next" "$config.upgrade-next"
    exit "$status"
}
trap finish EXIT
trap 'exit 1' TERM INT

( set -o noclobber; umask 022; : > "$guard" )
guard_owned=1
proxy_changed=1
set_config "$candidate_config"
systemctl reload caddy
health "$old_build"

active="$(python3 -B - "$state/workspace.sqlite3" <<'PY'
from pathlib import Path
import sqlite3
import sys
import time
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as db:
    print(db.execute("SELECT count(*) FROM staged_jobs WHERE state='running' AND lease_until>?",
                     (int(time.time()),)).fetchone()[0])
PY
)"
if [[ "$active" != 0 ]]; then
    echo 'A model task is still running; the upgrade can be retried after it finishes.' >&2
    exit 1
fi

service_changed=1
systemctl stop "$service"
snapshot before > "$backup/state.before.sha256"
tar -C "$(dirname "$state")" -czf "$backup/state.tar.gz" "$(basename "$state")"
switch_pointer "$new"
systemctl start "$service"
await_build "$new_build"
snapshot after > "$backup/state.after.sha256"
cmp -s "$backup/state.before.sha256" "$backup/state.after.sha256"
curl --silent --show-error --fail --max-time 6 \
    --connect-to site.example.invalid:443:127.0.0.1:443 \
    https://site.example.invalid/ | grep -F 'https://workbench.example.invalid/' >/dev/null
rm -f -- "$guard"
printf 'Active %s. Database preserved. Backup: %s\n' "$new_build" "$backup"
