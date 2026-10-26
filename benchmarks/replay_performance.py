"""Repeatable, isolated replay benchmarks; never starts a server or uses live data.

Run: python benchmarks/replay_performance.py --repeats 3 --events 20000
Use --profile /tmp/replay.prof for engine CPU call statistics.
"""
import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.replay.engine import ReplayEngine, config_from
from oms_client.replay.service import ReplaySession
from oms_client.replay.source import ReplaySource
from oms_client.replay.store import ReplayStore


def config(auto=False):
    return config_from(dict(
        ticker='XYZ', start='2026-10-26T12:00:00+09:00', end='2026-10-26T13:00:00+09:00',
        initial_krw='1000000', initial_coin='1000', fee_bps='5', quantity_step='0.01',
        min_notional='1', latency_ms=0, auto_mm=auto, queue_cancel_mode='probabilistic',
    ))


def book(at, quantity=100):
    return dict(time=at, kind='book', venue='bithumb', source_id=f'book:{at}', server_time=at,
                bids=[dict(price=100-i, quantity=quantity) for i in range(5)],
                asks=[dict(price=102+i, quantity=quantity) for i in range(5)])


def events(start, count):
    for index in range(1, count+1):
        at = start + index
        if index % 10 == 0:
            yield book(at, 100 + (index // 10) % 3 * 10)
        else:
            yield dict(time=at, kind='tick', venue='bithumb', source_id=f'tick:{at}',
                       price=100 if index % 2 else 102, qty=-2 if index % 2 else 2,
                       direction_known=True, server_time=at)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def engine_run(data, scenario):
    engine = ReplayEngine(config(auto=scenario == 'mm'))
    engine.on_event(book(engine.now))
    engine.start()
    if scenario == 'orders100':
        for index in range(100):
            engine.place('buy', str(100 - index % 5), '1')
        engine.advance(engine.now)
    checkpoints = []
    start = time.perf_counter()
    for index, event in enumerate(data):
        engine.on_event(event)
        if index % 1000 == 0:
            checkpoints.append(digest(engine.snapshot()))
    engine.finish()
    elapsed = time.perf_counter() - start
    # Hash at capture so later mutable books cannot change earlier checkpoints.
    result = dict(final=engine.snapshot(), fills=engine.new_fills,
                  orders=engine.new_order_events, checkpoints=checkpoints)
    return elapsed, digest(result)


class EmptySource:
    total = 0
    skipped = 0
    manifest = []

    def __init__(self, config):
        pass


def projection_run(iterations=100, http=False):
    with tempfile.TemporaryDirectory() as temp:
        session = ReplaySession('benchmark', config(True), ReplayStore(temp), EmptySource)
        session.engine.on_event(book(session.engine.now, 0))
        session.engine.start()
        for event in events(session.engine.now, 2000):
            session.engine.on_event(event)
        session._publish()
        if http:
            from flask import Flask
            from types import SimpleNamespace
            from oms_client.api.routes.replays import register
            app = Flask('replay-benchmark')
            manager = SimpleNamespace(get=lambda identifier: session.get(),
                                      get_encoded=lambda identifier: session.get_encoded(), store=session.store)
            register(app, manager)
            client = app.test_client()
        start = time.perf_counter()
        for _ in range(iterations):
            if http:
                response = client.get('/api/replays/benchmark')
                assert response.status_code == 200
            else:
                result = session.get()
        elapsed = time.perf_counter() - start
        if http:
            result = response.get_json()
        return elapsed, digest({key: result[key] for key in ('account', 'orders', 'fills', 'ticks')})


def source_run(count):
    import duckdb
    with tempfile.TemporaryDirectory() as temp:
        path = str(Path(temp) / 'books.parquet')
        cfg = config()
        con = duckdb.connect()
        con.execute('CREATE TABLE input AS SELECT ? + i AS desktopTime, ? + i AS serverTime, '
                    '100.0 AS b1_p, 10000.0 AS b1_q, 102.0 AS a1_p, 10200.0 AS a1_q '
                    'FROM range(?) t(i)', [cfg['start_ms'], cfg['start_ms'], count])
        con.sql('SELECT * FROM input').write_parquet(path)
        con.close()
        source = ReplaySource(cfg)
        try:
            start = time.perf_counter()
            result = list(source._stream('bithumb', 'book', [path], False, ['benchmark.parquet']))
            elapsed = time.perf_counter() - start
            return elapsed, digest(result)
        finally:
            source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=20000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--profile')
    parser.add_argument('--scenarios', nargs='+', default=['idle', 'orders100', 'mm', 'snapshot100', 'http100', 'source'],
                        choices=['idle', 'orders100', 'mm', 'snapshot100', 'http100', 'source'])
    args = parser.parse_args()
    if args.events < 1 or args.repeats < 1:
        parser.error('events/repeats must be positive')
    data = list(events(config()['start_ms'], args.events))
    results = {}
    for scenario in args.scenarios:
        samples = []
        hashes = set()
        for _ in range(args.repeats):
            if scenario in {'snapshot100', 'http100'}:
                elapsed, checksum = projection_run(http=scenario == 'http100')
            elif scenario == 'source':
                elapsed, checksum = source_run(args.events)
            else:
                elapsed, checksum = engine_run(data, scenario)
            samples.append(elapsed)
            hashes.add(checksum)
        assert len(hashes) == 1, (scenario, hashes)
        results[scenario] = dict(median_seconds=statistics.median(samples),
                                 samples_seconds=samples, sha256=hashes.pop())
    print(json.dumps(results, indent=2))
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(engine_run, data, 'mm')
        profiler.dump_stats(args.profile)


if __name__ == '__main__':
    main()
