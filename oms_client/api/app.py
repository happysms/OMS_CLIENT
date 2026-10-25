"""Build the HTTP application and connect feature routes to domain services."""
from datetime import timedelta
import os

from flask import Flask

from ..analysis import history as data_service
from ..paths import ROOT
from ..storage.state import StateStore
from . import errors
from .auth import TransportAwareSessionInterface, configure_auth
from .data_exports import DataExportJobs
from .jobs import AnalysisJobs
from .routes import analysis, backtesting, collectors, data, oms, paper_trading, workspace
from .services import configure_services


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.session_interface = TransportAwareSessionInterface()
    trusted_hosts = os.environ.get("DASHBOARD_TRUSTED_HOSTS", "")
    app.config.update(
        STATE_DIR=os.environ.get("DASHBOARD_STATE_DIR", str(ROOT / ".state")),
        PASSWORD=os.environ.get("DASHBOARD_PASSWORD"),
        PASSWORD_HASH=os.environ.get("DASHBOARD_PASSWORD_HASH"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="midprice_session",
        SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_SECURE_COOKIES") == "1",
        BEHIND_PROXY=os.environ.get("DASHBOARD_BEHIND_PROXY") == "1",
        TRUSTED_HOSTS=[host.strip() for host in trusted_hosts.split(",") if host.strip()] or None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=64 * 1024,
        JOB_WORKERS=1, JOB_MAX_PENDING=8, JOB_MAX_CACHED=16, JOB_TTL=3600,
        JOB_MAX_CSV_BYTES=256 * 1024 * 1024,
        DATA_EXPORT_MAX_PENDING=4, DATA_EXPORT_MAX_CACHED=8, DATA_EXPORT_TTL=3600,
        DATA_EXPORT_MAX_CSV_BYTES=256 * 1024 * 1024, DATA_EXPORT_MAX_ROWS=1_000_000,
    )
    if config:
        app.config.update(config)
    if app.config["BEHIND_PROXY"] and app.config["TRUSTED_HOSTS"] is None:
        app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", ".trycloudflare.com"]
    store = StateStore(app.config["STATE_DIR"])
    csrf_token = configure_auth(app, store)
    jobs = AnalysisJobs(
        store, app.config.get("ANALYZER", data_service.analyze),
        backtester=app.config.get("BACKTESTER"), workers=app.config["JOB_WORKERS"],
        max_pending=app.config["JOB_MAX_PENDING"], max_cached=app.config["JOB_MAX_CACHED"],
        ttl=app.config["JOB_TTL"], max_csv_bytes=app.config["JOB_MAX_CSV_BYTES"],
    )
    data_exports = DataExportJobs(
        store.directory, exporter=app.config.get("DATA_EXPORTER"),
        max_pending=app.config["DATA_EXPORT_MAX_PENDING"], max_cached=app.config["DATA_EXPORT_MAX_CACHED"],
        ttl=app.config["DATA_EXPORT_TTL"], max_bytes=app.config["DATA_EXPORT_MAX_CSV_BYTES"],
        max_rows=app.config["DATA_EXPORT_MAX_ROWS"],
    )
    app.extensions.update(state_store=store, analysis_jobs=jobs, data_export_jobs=data_exports)
    live_manager, paper_service = configure_services(app, store, jobs)
    errors.register(app)
    workspace.register(app, csrf_token)
    data.register(app, data_exports)
    analysis.register(app, jobs, store, live_manager)
    backtesting.register(app, jobs)
    paper_trading.register(app, paper_service)
    oms.register(app)
    collectors.register(app)
    return app
