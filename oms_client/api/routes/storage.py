"""Authenticated server disk status and opt-in market-file retention controls."""

from flask import g, jsonify, request

from ...storage.access import DATA_ACCESS


def register(app, service):
    @app.before_request
    def hold_market_data():
        if request.path.startswith('/api/') and not request.path.startswith('/api/storage'):
            lease = DATA_ACCESS.read()
            lease.__enter__()
            g.market_data_lease = lease

    @app.teardown_request
    def release_market_data(error):
        lease = g.pop('market_data_lease', None)
        if lease is not None:
            lease.__exit__(None, None, None)

    @app.get('/api/storage')
    def storage_status():
        return jsonify(service.status())

    @app.post('/api/storage/policy')
    def storage_policy():
        return jsonify(service.save_policy(request.get_json(silent=True)))

    @app.post('/api/storage/preview')
    def storage_preview():
        return jsonify(service.preview(request.get_json(silent=True))), 202
