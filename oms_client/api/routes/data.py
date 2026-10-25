"""Read-only market data discovery and stored time bounds."""
from flask import jsonify, request

from ...analysis import history
from ...market_data import parquet


def register(app):
    @app.get("/api/catalog")
    def catalog():
        result = history.catalog()
        result["mode"] = "server"
        return jsonify(result)

    @app.get("/api/bounds")
    def bounds():
        return jsonify(parquet.bounds(request.args.get("exchange"), request.args.get("ticker")))
