"""Asynchronous backtest submission, results and full trade exports."""
from flask import jsonify, request, send_file

from ..validation import _json_object


def register(app, jobs):
    @app.post("/api/backtests")
    def create_backtest():
        config = _json_object(request.get_json(silent=True))
        try:
            return jsonify(jobs.submit(config, kind="backtest")), 202
        except OverflowError as error:
            return jsonify(error=str(error)), 429

    @app.get("/api/backtests/<run_id>")
    def get_backtest(run_id):
        try:
            result = jobs.get(run_id)
            if result.get("kind", "analysis") != "backtest":
                raise KeyError(run_id)
            return jsonify(result)
        except KeyError:
            return jsonify(error="백테스트 기록을 찾지 못했습니다."), 404

    @app.get("/api/backtests/<run_id>/csv")
    def export_backtest(run_id):
        try:
            if jobs.get(run_id).get("kind", "analysis") != "backtest":
                raise KeyError(run_id)
            return send_file(jobs.csv(run_id), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"backtest-trades-{run_id}.csv")
        except KeyError:
            return jsonify(error="백테스트 기록을 찾지 못했습니다."), 404
        except (ValueError, FileNotFoundError):
            return jsonify(error="결과 보관 시간이 지났거나 백테스트가 끝나지 않았습니다. 다시 실행한 뒤 내보내 주세요."), 410
