#!/usr/bin/env bash
set -euo pipefail

oms_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
oms_python=${OMS_PYTHON:-"$(dirname -- "$oms_root")/.venv/bin/python"}
oms_state=${DASHBOARD_STATE_DIR:-"$oms_root/.state"}
oms_units=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
oms_tunnel=0
if [[ ${1:-} == --with-tunnel ]]; then
  oms_tunnel=1
elif [[ $# -gt 0 ]]; then
  printf 'Usage: bash deploy/install_user_services.sh [--with-tunnel]\n' >&2
  exit 2
fi
if [[ ! -x "$oms_python" ]]; then
  printf 'Set OMS_PYTHON to the virtualenv Python executable.\n' >&2
  exit 1
fi
if [[ $oms_tunnel == 1 && ! -x "$oms_root/.tools/cloudflared" ]]; then
  printf 'Install the official cloudflared binary at .tools/cloudflared first.\n' >&2
  exit 1
fi
mkdir -p -- "$oms_state" "$oms_units"
chmod 700 -- "$oms_state"

"$oms_python" - "$oms_root" "$oms_python" "$oms_state" "$oms_units" <<'PY'
import ipaddress, os, re, sqlite3, subprocess, sys
from pathlib import Path

root, python, state, units = map(Path, sys.argv[1:])
database = state / 'dashboard.sqlite3'
if not database.exists():
    raise SystemExit('Initialize your login first: python server.py --init-password')
with sqlite3.connect(database) as connection:
    if not connection.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone():
        raise SystemExit('Initialize your login first: python server.py --init-password')
hosts = ['localhost', '127.0.0.1', '0.0.0.0', '.trycloudflare.com']
try:
    route = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True, text=True, timeout=3, check=True).stdout
    match = re.search(r'\bsrc\s+(\S+)', route)
    if match:
        hosts.append(str(ipaddress.IPv4Address(match.group(1))))
except (OSError, ValueError, subprocess.SubprocessError):
    pass
trusted = os.environ.get('OMS_TRUSTED_HOSTS', ','.join(hosts))
revision = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
values = {'DASHBOARD_STATE_DIR': str(state.resolve()), 'DASHBOARD_DATA_ROOT': os.environ.get('DASHBOARD_DATA_ROOT', '/data'),
          'DASHBOARD_BEHIND_PROXY': '1', 'DASHBOARD_TRUSTED_HOSTS': trusted, 'OMS_CLIENT_REVISION': revision}
def quote(value):
    if '\n' in value or '\r' in value:
        raise SystemExit('Service environment values must be single-line strings')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
environment = state / 'service.env'
environment.write_text(''.join(f'{key}={quote(value)}\n' for key, value in values.items()))
environment.chmod(0o600)
replacements = {'@APP_ROOT@': str(root.resolve()), '@PYTHON@': str(python.absolute()), '@STATE_ROOT@': str(state.resolve())}
for filename in ['oms-client.service', 'oms-client-route.service']:
    content = (root / 'deploy' / (filename + '.in')).read_text()
    for token, value in replacements.items():
        # Percent signs in literal paths must not be interpreted as unit specifiers.
        content = content.replace(token, value.replace('%', '%%'))
    target = units / filename
    target.write_text(content)
    target.chmod(0o644)
print('Installed service definitions at', units)
PY

systemctl --user daemon-reload
systemctl --user enable oms-client.service
systemctl --user restart oms-client.service
if [[ $oms_tunnel == 1 ]]; then
  systemctl --user enable --now oms-client-route.service
fi
systemctl --user is-active oms-client.service
