"""Simulation-only account, order and reset endpoints."""
from flask import jsonify, request

from ..validation import _json_object


def register(app, paper_service):
    @app.get("/api/paper")
    def paper_snapshot():
        try:
            return jsonify(paper_service().snapshot())
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/orders")
    def paper_order():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().place_order(payload)), 201
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/reset")
    def paper_reset():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().reset(payload)), 200
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
