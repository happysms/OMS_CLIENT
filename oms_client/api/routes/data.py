"""Read-only market data discovery and stored time bounds."""
from flask import jsonify, request, send_file

from ...analysis import history
from ...market_data import candles, funding, parquet
from ...market_data.validation import DataError


def register(app, exports):
    @app.get("/api/catalog")
    def catalog():
        result = history.catalog()
        result["mode"] = "server"
        return jsonify(result)

    @app.get("/api/bounds")
    def bounds():
        data_type = request.args.get("data_type", "price")
        if data_type == "funding":
            return jsonify(funding.bounds(request.args.get("exchange"), request.args.get("ticker")))
        if data_type == "candle":
            return jsonify(candles.bounds(request.args.get("exchange"), request.args.get("ticker")))
        if data_type != "price":
            raise DataError("소스 유형은 가격, 펀딩비율 또는 1분봉 종가로 선택해 주세요.")
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
