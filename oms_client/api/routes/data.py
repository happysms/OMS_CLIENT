"""Read-only market data discovery and stored time bounds."""
from flask import jsonify, request, send_file

from ...analysis import history
from ...market_data import parquet


def register(app, exports):
    @app.get("/api/catalog")
    def catalog():
        result = history.catalog()
        result["mode"] = "server"
        return jsonify(result)

    @app.get("/api/bounds")
    def bounds():
        return jsonify(parquet.bounds(request.args.get("exchange"), request.args.get("ticker")))

    @app.post("/api/data/exports")
    def create_data_export():
        try:
            return jsonify(exports.submit(request.get_json(silent=True))), 202
        except OverflowError as error:
            return jsonify(error=str(error)), 429

    @app.get("/api/data/exports/<export_id>")
    def data_export_status(export_id):
        try:
            return jsonify(exports.get(export_id))
        except KeyError:
            return jsonify(error="CSV 내보내기 기록이 없거나 보관 시간이 지났습니다. 다시 내보내 주세요."), 404

    @app.get("/api/data/exports/<export_id>/csv")
    def download_data_export(export_id):
        try:
            path, name = exports.csv(export_id)
            return send_file(path, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=name)
        except KeyError:
            return jsonify(error="CSV 보관 시간이 지났거나 기록이 없습니다. 다시 내보내 주세요."), 404
        except (ValueError, FileNotFoundError) as error:
            return jsonify(error=str(error)), 410
