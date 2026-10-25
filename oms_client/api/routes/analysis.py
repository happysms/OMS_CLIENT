"""Historical/live analysis, exports, presets and run history endpoints."""
import re

from flask import jsonify, request, send_file

from ...analysis.formulas import formula_ids
from ...market_data.validation import DataError
from ..validation import _query_config


def register(app, jobs, store, live_manager):
    @app.post("/api/analyses")
    def create_analysis():
        config = _query_config(request.get_json(silent=True))
        try:
            return jsonify(jobs.submit(config)), 202
        except OverflowError as error:
            return jsonify(error=str(error)), 429

    @app.get("/api/analyses/<run_id>")
    def get_analysis(run_id):
        try:
            result = jobs.get(run_id)
            if result.get("kind", "analysis") != "analysis":
                raise KeyError(run_id)
            return jsonify(result)
        except KeyError:
            return jsonify(error="분석 기록을 찾지 못했습니다."), 404

    @app.get("/api/analyses/<run_id>/csv")
    def export_analysis(run_id):
        try:
            if jobs.get(run_id).get("kind", "analysis") != "analysis":
                raise KeyError(run_id)
            return send_file(jobs.csv(run_id), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"midprice-{run_id}.csv")
        except KeyError:
            return jsonify(error="분석 기록을 찾지 못했습니다."), 404
        except ValueError as error:
            return jsonify(error=str(error)), 410
        except FileNotFoundError:
            return jsonify(error="결과 보관 시간이 지났습니다. 다시 분석한 뒤 내보내 주세요."), 410

    @app.get("/api/history")
    def history():
        return jsonify(items=jobs.history())

    @app.route("/api/presets", methods=["GET", "POST"])
    def presets():
        if request.method == "GET":
            return jsonify(items=store.presets())
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not 1 <= len(payload["name"].strip()) <= 80:
            return jsonify(error="조합 이름을 1~80자로 입력해 주세요."), 400
        if payload.get("id") is not None and (not isinstance(payload["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", payload["id"])):
            return jsonify(error="저장한 조합 ID를 확인해 주세요."), 400
        config = _query_config(payload.get("config"))
        try:
            return jsonify(store.save_preset(payload["name"].strip(), config, payload.get("id"))), 201
        except KeyError:
            return jsonify(error="저장한 조합을 찾지 못했습니다."), 404
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.delete("/api/presets/<preset_id>")
    def delete_preset(preset_id):
        try:
            store.delete_preset(preset_id)
            return jsonify(ok=True)
        except KeyError:
            return jsonify(error="저장한 조합을 찾지 못했습니다."), 404

    @app.post("/api/live")
    def create_live():
        config = _query_config(request.get_json(silent=True))
        referenced = set(formula_ids(config["formula"]))
        if any(leg.get("data_type") == "funding" and leg.get("id") in referenced for leg in config["legs"]):
            raise DataError("펀딩비율은 저장된 데이터 조회와 과거 분석에서 사용할 수 있습니다. 과거 분석으로 전환해 주세요.")
        return jsonify(id=live_manager().start(config), status="running"), 202

    @app.get("/api/live/<live_id>")
    def get_live(live_id):
        try:
            return jsonify(live_manager().get(live_id))
        except KeyError:
            return jsonify(error="실시간 분석을 찾지 못했습니다."), 404

    @app.delete("/api/live/<live_id>")
    def stop_live(live_id):
        try:
            live_manager().stop(live_id)
            return jsonify(ok=True)
        except KeyError:
            return jsonify(error="실시간 분석을 찾지 못했습니다."), 404
