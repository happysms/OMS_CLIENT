"""Report the currently unconnected OMS integration honestly."""


def connection_status():
    return {
        "status": "planned",
        "connected": False,
        "live_orders": False,
        "execution_location": "remote",
        "protocol_version": "oms.remote.v1",
        "contract": "docs/remote_execution_socket.ko.md",
        "message": "실제 매매는 별도 실행 서버에서 수행합니다. 원격 소켓 연결은 정의서 작성 완료, 구현 대기 상태입니다.",
    }
