"""Simulation-only account, order and reset endpoints."""
import csv
import io
import json

from flask import jsonify, request, Response, stream_with_context

from ..validation import _json_object
from ...paper_trading.currency import USDT_CSV_FIELDS, usdt_csv_values


def register(app, paper_service):
    @app.get('/api/paper/fills')
    def paper_fills():
        try:
            before = request.args.get('before')
            return jsonify(paper_service().fills(before=int(before) if before is not None else None,
                                                 limit=int(request.args.get('limit', '100'))))
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.get('/api/paper/fills/csv')
    def paper_fill_csv():
        service = paper_service()
        fields = ['sequence', 'fill_id', 'timestamp_ms', 'exchange', 'ticker', 'side', 'price', 'quantity', 'fee',
                  'realized_pnl', 'realized_pnl_cumulative', 'unrealized_pnl', 'theory_unrealized_pnl',
                  'total_pnl', 'theory_total_pnl', 'cash', 'equity', 'theory_equity', 'fees_paid',
                  'valuation_status', 'valuation_json'] + USDT_CSV_FIELDS

        def generate():
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(fields)
            yield '\ufeff' + output.getvalue()
            with service.store.connection() as connection:
                for row in connection.execute('SELECT sequence,report FROM paper_fill_reports ORDER BY sequence'):
                    report = json.loads(row['report'])
                    fill, account = report['fill'], report.get('account') or {}
                    output.seek(0); output.truncate(0)
                    writer.writerow([row['sequence'], fill['id'], fill['timestamp'],
                                     *[fill[key] for key in ('exchange','ticker','side','price','quantity','fee','realized_pnl')],
                                     *[account.get(key) for key in ('realized_pnl','unrealized_pnl','theory_unrealized_pnl',
                                                                   'total_pnl','theory_total_pnl','cash','equity','theory_equity','fees_paid')],
                                     report['valuation_status'], row['report']] + usdt_csv_values(report))
                    yield output.getvalue()
        return Response(stream_with_context(generate()), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=paper-fills.csv'})

    @app.get("/api/paper")
    def paper_snapshot():
        try:
            return jsonify(paper_service().snapshot())
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/orders")
    def paper_order():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().place_order(payload)), 201
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/reset")
    def paper_reset():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().reset(payload)), 200
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
