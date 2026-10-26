"""Authenticated registration of versioned strategy configurations."""

from flask import abort, jsonify, request
from ...market_data.validation import DataError
from ...strategies.registry import templates


def register(app, registry):
    def invoke(call, *args):
        try:
            return call(*args)
        except KeyError:
            abort(404, description='전략 또는 버전을 찾을 수 없습니다.')
        except (ValueError, TypeError) as exc:
            raise DataError(str(exc)) from exc

    @app.get('/api/strategies')
    def strategy_list():
        return jsonify(items=registry.list(), templates=templates())

    @app.post('/api/strategies')
    def strategy_create():
        return jsonify(invoke(registry.save, request.get_json(silent=True))), 201

    @app.get('/api/strategies/<identifier>')
    def strategy_get(identifier):
        version = request.args.get('revision')
        if version is not None and (
            not version.isascii() or not version.isdigit() or len(version) > 7
        ):
            raise DataError('전략 버전을 확인하세요.')
        return jsonify(
            invoke(registry.get, identifier, int(version) if version is not None else None)
        )

    @app.post('/api/strategies/<identifier>/revisions')
    def strategy_update(identifier):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise DataError('전략 설정을 확인하세요.')
        payload = dict(payload)
        expected = payload.pop('expected_revision', None)
        return jsonify(invoke(registry.save, payload, identifier, expected)), 201

    @app.post('/api/strategies/<identifier>/archive')
    def strategy_archive(identifier):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'archived'}:
            raise DataError('보관 설정을 확인하세요.')
        return jsonify(invoke(registry.archive, identifier, payload['archived']))
