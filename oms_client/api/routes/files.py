"""Authenticated read-only market-file browsing; no raw path/download routes."""

from flask import jsonify, request

from ...market_data import file_browser


def register(app):
    @app.get("/api/data/files")
    def data_files():
        return jsonify(
            file_browser.list_files(
                request.args.get("node"),
                offset=request.args.get("offset", 0),
                limit=request.args.get("limit", 100),
                q=request.args.get("q"),
            )
        )

    @app.get("/api/data/files/info")
    def data_file_info():
        return jsonify(file_browser.file_info(request.args.get("node")))
