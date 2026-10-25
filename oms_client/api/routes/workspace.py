"""Workspace shell, public health, session metadata and allowed assets."""
import os

from flask import abort, jsonify, send_from_directory

from ...paths import FRONTEND as STATIC

# Only browser assets are served. Source, docs and runtime files stay private.
STATIC_FILES = {
    "app.js", "styles.css",
    "shell/bootstrap.js", "shell/tabs.js", "shell/workspace.js",
    "shared/api.js", "shared/charts.js", "shared/dom.js", "shared/jobs.js",
    "shared/source-controls.js",
    "features/analysis/controller.js", "features/data/controller.js",
    "features/funding/controller.js",
    "features/files/controller.js",
    "features/quality/controller.js",
    "features/backtesting/controller.js", "features/paper_trading/controller.js",
}
MODULES = [
    {"id": "data", "label": "데이터 확인", "status": "ready"},
    {"id": "quality", "label": "데이터 품질", "status": "ready"},
    {"id": "funding", "label": "펀딩비율", "status": "ready"},
    {"id": "analysis", "label": "분석", "status": "ready"},
    {"id": "backtest", "label": "백테스트", "status": "ready"},
    {"id": "paper", "label": "모의투자", "status": "ready"},
    {"id": "oms", "label": "OMS", "status": "planned"},
]



def register(app, csrf_token):
    @app.get("/api/health")
    def health():
        result = {"ok": True, "service": "oms-client"}
        if os.environ.get("OMS_CLIENT_REVISION"):
            result["revision"] = os.environ["OMS_CLIENT_REVISION"]
        return jsonify(result)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/<path:filename>")
    def static_file(filename):
        if filename not in STATIC_FILES:
            abort(404)
        return send_from_directory(STATIC, filename)

    @app.get("/api/session")
    def session_information():
        return jsonify(authenticated=True, user="admin", csrf_token=csrf_token(), mode="server", capabilities={"server_analysis": True, "live": True, "saved_presets": True, "backtest": True, "paper": True, "funding": True, "funding_live": False, "candles": True, "candle_live": False, "live_orders": False})

    @app.get("/api/version")
    def version_information():
        return jsonify(application="OMS_CLIENT", revision=os.environ.get("OMS_CLIENT_REVISION") or "development", modules=MODULES)

    @app.get("/api/modules")
    def modules():
        return jsonify(items=MODULES)
