"""Authenticated explicit manual trading actions; no private request during page loading."""
import threading
from flask import jsonify, request
from ...manual_trading.service import ManualTradingService, OrderNotSubmitted

def register(app, directory):
    creation_lock = threading.Lock()
    command_slot = threading.BoundedSemaphore(1)

    def service():
        with creation_lock:
            if 'manual_trading' not in app.extensions:
                factory = app.config.get('MANUAL_TRADING_FACTORY', ManualTradingService)
                app.extensions['manual_trading'] = factory(directory)
            return app.extensions['manual_trading']

    def command(callback):
        if not command_slot.acquire(blocking=False):
            return (jsonify(error='다른 수동 매매 요청을 처리 중입니다. 상태 확인 후 다시 시도하세요.'), 429)
        try:
            return jsonify(callback())
        except OrderNotSubmitted as error:
            return (jsonify(error=str(error), order_not_submitted=True), 400)
        except ValueError as error:
            return (jsonify(error=str(error)), 400)
        except RuntimeError as error:
            return (jsonify(error=str(error)), 409)
        except Exception:
            return (jsonify(error='요청을 완료하지 못했습니다. 실주문은 재전송 전에 주문 목록과 상태를 확인하세요.'), 503)
        finally:
            command_slot.release()

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('요청 내용을 확인하세요.')
        return value

    @app.get('/api/manual')
    def snapshot():
        try:
            response = jsonify(service().snapshot(request.args.get('exchange', 'bithumb')))
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as error:
            return (jsonify(error=str(error)), 400)
        except Exception:
            return (jsonify(error='수동 매매 저장소를 열지 못했습니다.'), 503)

    @app.post('/api/manual/market')
    def subscribe():

        def run():
            data = payload()
            if set(data) != {'exchange', 'ticker'}:
                raise ValueError('시세 구독 항목을 확인하세요.')
            service().market.subscribe(data['exchange'], data['ticker'])
            return dict(status='connecting')
        return command(run)

    @app.post('/api/manual/keys')
    def add_key():
        return command(lambda: service().register_key(payload()))

    @app.post('/api/manual/keys/<identifier>/verify')
    def verify(identifier):
        return command(lambda: service().verify_key(identifier))

    @app.delete('/api/manual/keys/<identifier>')
    def delete_key(identifier):

        def run():
            service().delete_key(identifier)
            return dict(deleted=True)
        return command(run)

    @app.post('/api/manual/orders')
    def order():
        return command(lambda: service().submit(payload()))

    @app.post('/api/manual/orders/<key_id>/<client_id>/<action>')
    def order_action(key_id, client_id, action):
        return command(lambda: service().order_action(key_id, client_id, action))
