"""Read-only market data discovery, ticks, and stored time bounds."""
import threading

from flask import jsonify, request, send_file

from ...analysis import history
from ...market_data import candles, funding, parquet, ticks
from ...market_data.validation import DataError


def register(app, exports):
    tick_slots = threading.BoundedSemaphore(2)

    @app.get("/api/catalog")
    def catalog():
        result = history.catalog()
        result["tick_exchanges"] = ticks.list_symbols()
        result["mode"] = "server"
        return jsonify(result)

    @app.get("/api/bounds")
    def bounds():
        data_type = request.args.get("data_type", "price")
        if data_type == "funding":
            return jsonify(funding.bounds(request.args.get("exchange"), request.args.get("ticker")))
        if data_type == "candle":
            return jsonify(candles.bounds(request.args.get("exchange"), request.args.get("ticker")))
        if data_type == "tick":
            return jsonify(ticks.bounds(request.args.get("exchange"), request.args.get("ticker")))
        if data_type != "price":
            raise DataError("소스 유형은 호가, 체결 틱, 펀딩비율 또는 1분봉 종가로 선택해 주세요.")
        return jsonify(parquet.bounds(request.args.get("exchange"), request.args.get("ticker")))

    @app.post("/api/data/ticks")
    def query_ticks():
        if not tick_slots.acquire(blocking=False):
            return jsonify(error="체결 틱 조회가 진행 중입니다. 잠시 후 다시 시도해 주세요."), 429
        try:
            return jsonify(ticks.query(request.get_json(silent=True)))
        finally:
            tick_slots.release()

    @app.post("/api/data/ticks/csv")
    def download_ticks():
        if not tick_slots.acquire(blocking=False):
            return jsonify(error="체결 틱 조회가 진행 중입니다. 잠시 후 다시 시도해 주세요."), 429
        stream = None
        try:
            stream, name, size = ticks.csv_file(request.get_json(silent=True),
                max_rows=app.config["DATA_EXPORT_MAX_ROWS"], max_bytes=app.config["DATA_EXPORT_MAX_CSV_BYTES"])
            response = send_file(stream, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=name)
            response.content_length = size
            response.call_on_close(stream.close)
            return response
        except BaseException:
            if stream is not None:
                stream.close()
            raise
        finally:
            tick_slots.release()

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
