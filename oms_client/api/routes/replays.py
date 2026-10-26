"""Historical simulation controls and frozen per-fill reporting."""
import csv
import io
import json

from flask import Response, abort, jsonify, request, stream_with_context

from ...market_data.validation import DataError


def register(app, manager):
    def invoke(call, *args):
        try:
            return call(*args)
        except KeyError:
            abort(404, description='시뮬레이션을 찾을 수 없습니다.')
        except (ValueError, TypeError) as exc:
            raise DataError(str(exc)) from exc

    @app.get('/api/replays')
    def replay_list():
        before = request.args.get('before')
        if before is not None and (not before.isascii() or not before.isdigit() or len(before)>18 or int(before)<1):
            raise DataError('계좌 목록 페이지 번호를 확인하세요.')
        items = manager.store.recent(int(before) if before is not None else None, 21)
        return jsonify(items=items[:20], next_before=items[19]['history_sequence'] if len(items)>20 else None)

    @app.post('/api/replays')
    def replay_create():
        return jsonify(invoke(manager.create, request.get_json(silent=True))), 201

    @app.get('/api/replays/<identifier>')
    def replay_get(identifier):
        return jsonify(invoke(manager.get, identifier))

    @app.post('/api/replays/<identifier>/control')
    def replay_control(identifier):
        return jsonify(invoke(manager.control, identifier, request.get_json(silent=True)))

    @app.get('/api/replays/<identifier>/fills')
    def replay_fills(identifier):
        before = request.args.get('before')
        if before is not None and (not before.isdigit() or not 1<=int(before)<=100001):
            raise DataError('체결 페이지 번호를 확인하세요.')
        rows = invoke(manager.store.fills, identifier, int(before) if before is not None else None)
        return jsonify(items=rows, next_before=rows[-1]['sequence'] if len(rows)==100 else None)

    @app.get('/api/replays/<identifier>/fills.csv')
    def replay_csv(identifier):
        # Freeze the upper bound so a running export cannot add or duplicate later fills.
        snapshot = invoke(manager.get, identifier)
        @stream_with_context
        def generate():
            output = io.StringIO(); writer = csv.writer(output)
            writer.writerow(['sequence','virtual_time_ms','order_id','side','price','quantity','fee','fill_realized_pnl',
                             'cumulative_realized_pnl','unrealized_pnl','theory_unrealized_pnl','total_pnl',
                             'theory_total_pnl','cash','coin','equity','source_tick'])
            yield '\ufeff'+output.getvalue(); output.seek(0); output.truncate(0)
            before = snapshot['fill_count']+1
            while before>1:
                rows = manager.store.fills(identifier, before, 500)
                if not rows: break
                for fill in rows:
                    account = fill['account']
                    writer.writerow([fill[key] for key in ('sequence','timestamp','order_id','side','price','quantity','fee','realized_pnl')]+
                                    [account.get(key) for key in ('realized_pnl','unrealized_pnl','theory_unrealized_pnl','total_pnl',
                                                                  'theory_total_pnl','cash','coin','equity')]+[fill['source_tick']])
                yield output.getvalue(); output.seek(0); output.truncate(0)
                before = rows[-1]['sequence']
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition':f'attachment; filename="replay-{snapshot["id"]}-fills.csv"'})

    @app.get('/api/replays/<identifier>/result.json')
    def replay_result(identifier):
        # Bound the export at one persisted checkpoint, including interrupted runs.
        snapshot = invoke(manager.store.get, identifier)
        snapshot.pop('fills', None)
        @stream_with_context
        def generate():
            yield json.dumps(snapshot, ensure_ascii=False, allow_nan=False)[:-1]+',"fills":['
            before, first = snapshot['fill_count']+1, True
            while before>1:
                rows = manager.store.fills(identifier, before, 500)
                if not rows: break
                for fill in rows:
                    yield ('' if first else ',')+json.dumps(fill, ensure_ascii=False, allow_nan=False)
                    first = False
                before = rows[-1]['sequence']
            yield ']}'
        return Response(generate(), mimetype='application/json', headers={
            'Content-Disposition':f'attachment; filename="replay-{snapshot["id"]}-result.json"'})
