"""Authenticated clock-offset queries isolated from order and market writers."""

import threading

from flask import jsonify, request

from ...market_data import quality, quality_live


def register(app):
    history_slot = threading.BoundedSemaphore(1)
    live_slots = threading.BoundedSemaphore(2)

    @app.get('/api/quality/catalog')
    def quality_catalog():
        return jsonify(quality.catalog())

    @app.post('/api/quality/history')
    def quality_history():
        if not history_slot.acquire(blocking=False):
            return (
                jsonify(error='다른 데이터 품질 조회를 계산 중입니다. 잠시 후 다시 조회해 주세요.'),
                429,
            )
        try:
            return jsonify(
                app.config.get('QUALITY_HISTORY_ANALYZER', quality.query_history)(
                    request.get_json(silent=True)
                )
            )
        finally:
            history_slot.release()

    @app.post('/api/quality/live')
    def quality_current():
        if not live_slots.acquire(blocking=False):
            return jsonify(error='현재 모니터 조회가 많습니다. 잠시 후 다시 시도해 주세요.'), 429
        try:
            return jsonify(
                app.config.get('QUALITY_LIVE_ANALYZER', quality_live.query_live)(
                    request.get_json(silent=True)
                )
            )
        finally:
            live_slots.release()
