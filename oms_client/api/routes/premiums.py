"""Authenticated durable all-symbol premium summaries and hourly history."""
import csv
import io

from flask import Response, jsonify, request

from ...analysis.premiums import completed_end
from ...market_data.validation import DataError, IDENTIFIER


def register(app, service):
    @app.get('/api/premiums')
    def premium_summary():
        return jsonify(service.status())

    @app.post('/api/premiums/refresh')
    def premium_refresh():
        service.request_refresh()
        return jsonify(status='queued'), 202

    @app.post('/api/premiums/settings')
    def premium_settings():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'enabled'}:
            raise DataError('enabled 설정을 입력해 주세요.')
        service.set_enabled(payload['enabled'])
        return jsonify(enabled=payload['enabled'])

    @app.get('/api/premiums/hourly')
    def premium_hourly():
        ticker = request.args.get('ticker', '').upper()
        if not IDENTIFIER.fullmatch(ticker) or len(ticker) > 80:
            raise DataError('시간별 괴리를 조회할 종목을 선택해 주세요.')
        end = completed_end(service.clock() * 1000)
        return jsonify(service.store.hourly(ticker, end))

    @app.get('/api/premiums/csv')
    def premium_csv():
        result = service.status()
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['ticker', 'end_ms', 'mean_3d_pct', 'samples_3d', 'coverage_3d_pct',
                         'mean_7d_pct', 'samples_7d', 'coverage_7d_pct', 'error_hours', 'profile'])
        for item in result['items']:
            ticker = item['ticker']
            if ticker.startswith(('=', '+', '-', '@')):
                ticker = "'" + ticker
            writer.writerow([ticker, result['end_ms'], *[item['windows'][str(days)][key]
                             for days in (3, 7) for key in ('mean_pct', 'samples', 'coverage_pct')],
                             item['error_hours'], result['profile']])
        return Response('\ufeff' + output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=premium-averages.csv'})
