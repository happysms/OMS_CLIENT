#!/usr/bin/env bash
set -euo pipefail

oms_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
oms_python=${OMS_PYTHON:-"$(dirname -- "$oms_root")/.venv/bin/python"}
oms_state=${DASHBOARD_STATE_DIR:-"$oms_root/.state"}
oms_data=${DASHBOARD_COLLECTOR_DATA_ROOT:-"$oms_root/data/market"}
oms_units=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
if [[ $# -gt 0 ]]; then
  printf 'Usage: bash deploy/install_collector_service.sh\n' >&2
  exit 2
fi
if [[ ! -x "$oms_python" ]]; then
  printf 'Set OMS_PYTHON to the virtualenv Python executable.\n' >&2
  exit 1
fi

"$oms_python" - "$oms_root" "$oms_python" "$oms_state" "$oms_data" "$oms_units" <<'PY'
from pathlib import Path
import sys

root, python, state, data, units = map(lambda value: Path(value).expanduser().absolute(), sys.argv[1:])
collector_state = state / 'collectors' / 'usdt'
for directory in (state, collector_state, data, units):
    directory.mkdir(parents=True, exist_ok=True)
state.chmod(0o700)
collector_state.chmod(0o700)
replacements = {'@APP_ROOT@': root, '@PYTHON@': python, '@DATA_ROOT@': data,
                '@COLLECTOR_STATE@': collector_state}
content = (root / 'deploy' / 'oms-usdt-collector.service.in').read_text()
for token, path in replacements.items():
    value = str(path)
    if any(character in value for character in ('\n', '\r', '\x00')):
        raise SystemExit('Service paths must be single-line strings')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    content = content.replace(token, value)
target = units / 'oms-usdt-collector.service'
target.write_text(content)
target.chmod(0o644)
print('Installed', target)
print('Market data:', data)
PY

systemd-analyze --user verify "$oms_units/oms-usdt-collector.service"
systemctl --user daemon-reload
systemctl --user enable oms-usdt-collector.service
systemctl --user restart oms-usdt-collector.service
systemctl --user is-active oms-usdt-collector.service
