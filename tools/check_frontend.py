"""Run DOM regressions offline with a fresh temporary strategy registry per suite.

Usage: python tools/check_frontend.py --jsdom /path/to/jsdom/lib/api.js
Individual suites: --scenarios fill-reports pnl-currency
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oms_client.strategies.registry import StrategyRegistry, templates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jsdom', default=os.environ.get('OMS_JSDOM_MODULE'))
    parser.add_argument('--scenarios', nargs='+')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    directory = ROOT / 'tests/frontend'
    files = sorted((directory / 'scenarios').glob('*.mjs')) + [directory / 'manual_trading.mjs']
    if args.scenarios:
        unknown = set(args.scenarios) - {p.stem for p in files}
        if unknown:
            parser.error('Unknown scenarios: ' + ', '.join(sorted(unknown)))
        files = [p for p in files if p.stem in args.scenarios]
    results = []
    with tempfile.TemporaryDirectory(prefix='oms-dom-') as temporary:
        registry = StrategyRegistry(Path(temporary) / 'state')
        catalog = Path(temporary) / 'catalog.json'
        catalog.write_text(json.dumps(dict(items=registry.list(), templates=templates())))
        env = dict(os.environ, OMS_STRATEGY_CATALOG=str(catalog))
        if args.jsdom:
            env['OMS_JSDOM_MODULE'] = str(Path(args.jsdom).resolve())
        for path in files:
            started = time.monotonic()
            try:
                process = subprocess.run(
                    ['node', '--import', str(directory / 'no_network.mjs'), str(path)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                status, output = process.returncode, process.stdout + process.stderr
            except subprocess.TimeoutExpired as error:
                status = 'timeout'
                output = (error.stdout or b'').decode() + (error.stderr or b'').decode()
            result = dict(
                scenario=path.stem,
                status=status,
                seconds=round(time.monotonic() - started, 3),
                output=output,
            )
            results.append(result)
            print(('PASS' if status == 0 else 'FAIL') + ' ' + path.stem, flush=True)
            if status != 0:
                print(output, flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(f'{sum(item["status"]==0 for item in results)}/{len(results)} DOM scenarios passed.')
    return int(any(item['status'] != 0 for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
