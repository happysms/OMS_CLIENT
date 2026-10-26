#!/usr/bin/env python3
"""Run one resumable premium catch-up, or keep accumulating hourly."""
import argparse
import json
import os
import signal
import threading

from oms_client.analysis.premiums import PremiumService
from oms_client.paths import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--state-dir', default=os.environ.get('DASHBOARD_STATE_DIR', str(ROOT / '.state'))
    )
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    service = PremiumService(args.state_dir, autostart=False)
    stopped = threading.Event()

    def stop(*_):
        service.stop_event.set()
        service.wake.set()
        stopped.set()

    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, stop)
    try:
        if args.once:
            service.refresh()
            print(json.dumps(service.store.get('job'), ensure_ascii=False))
            return 0 if service.store.get('job', {}).get('state') == 'complete' else 1
        service._loop()
    finally:
        service.close()


if __name__ == '__main__':
    raise SystemExit(main())
