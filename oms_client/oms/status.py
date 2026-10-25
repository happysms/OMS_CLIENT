"""Report the currently unconnected OMS integration honestly."""

def connection_status():
    return {
        "status": "planned", "connected": False, "live_orders": False,
        "message": "OMS 연결과 실주문 제어는 이후 모듈로 추가됩니다.",
    }
