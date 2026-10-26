"""Consistent JSON errors at the HTTP boundary."""

from flask import jsonify, request

from ..analysis import history as data_service


def register(app):
    @app.errorhandler(data_service.DataError)
    def data_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="조회 설정이 너무 큽니다."), 413

    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(503)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error.get_response()
