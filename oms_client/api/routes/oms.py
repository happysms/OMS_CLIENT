"""OMS integration status endpoints."""

from flask import jsonify

from ...oms.status import connection_status


def register(app):
    @app.get("/api/oms/status")
    def oms_status():
        return jsonify(connection_status())
