"""Lazy service ownership and orderly shutdown for one web application."""
import threading

from flask import abort


def configure_services(app, store, jobs):
    if app.config.get("LIVE_MANAGER") is not None:
        app.extensions["live_manager"] = app.config["LIVE_MANAGER"]
    if app.config.get("PAPER_SERVICE") is not None:
        app.extensions["paper_service"] = app.config["PAPER_SERVICE"]
    live_lock = threading.Lock()
    paper_lock = threading.Lock()

    def live_manager():
        with live_lock:
            if "live_manager" not in app.extensions:
                try:
                    from ..analysis.live import LiveAnalysisManager
                except ImportError:
                    abort(503, description="실시간 분석 모듈이 아직 준비되지 않았습니다.")
                app.extensions["live_manager"] = LiveAnalysisManager()
            return app.extensions["live_manager"]

    def paper_service():
        with paper_lock:
            if "paper_service" not in app.extensions:
                from ..paper_trading.service import PaperTradingService
                app.extensions["paper_service"] = PaperTradingService(store)
            return app.extensions["paper_service"]

    def close_services():
        app.extensions['retention_service'].close()
        app.extensions['premium_service'].close()
        app.extensions['replay_manager'].close()
        manager = app.extensions.get("live_manager")
        if manager:
            manager.close()
        paper = app.extensions.get("paper_service")
        if paper and hasattr(paper, "close"):
            paper.close()
        app.extensions["data_export_jobs"].close()
        jobs.close()

    app.extensions["close_services"] = close_services
    return live_manager, paper_service
