"""The pairing endpoints on a real loopback socket: three routes, bounded requests."""

import asyncio
import logging

import httpx
import pytest
import pytest_asyncio

from kalinka_plugin_qobuz.connect import receiver as receiver_module
from kalinka_plugin_qobuz.connect.receiver import HandoffReceiver

from conftest import assert_no_secrets, handoff_body


class _Handlers:
    def __init__(self, fail: bool = False):
        self.bodies = []
        self.fail = fail

    def display_info(self):
        return {"type": "STREAMER", "friendly_name": "Kalinka (test)"}

    def connect_info(self):
        return {"current_session_id": "", "app_id": "123456789"}

    async def handoff(self, body):
        if self.fail:
            raise RuntimeError("handler broke")
        self.bodies.append(body)
        return 200, {}


@pytest_asyncio.fixture
async def served():
    handlers = _Handlers()
    receiver = HandoffReceiver(handlers, host="127.0.0.1", port=0)
    await receiver.start()
    base = f"http://127.0.0.1:{receiver.port}"
    yield receiver, handlers, base
    await receiver.stop()


async def _raw(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return response


@pytest.mark.asyncio
async def test_display_and_connect_info_answer_json(served):
    receiver, _, base = served
    async with httpx.AsyncClient() as http:
        display = await http.get(base + "/streamcore/get-display-info")
        connect = await http.get(base + "/streamcore/get-connect-info?x=1")

    assert display.json() == {"type": "STREAMER", "friendly_name": "Kalinka (test)"}
    assert connect.json()["app_id"] == "123456789"
    assert display.headers["content-type"] == "application/json"
    assert display.headers["connection"] == "close"


@pytest.mark.asyncio
async def test_the_handoff_body_reaches_the_handler(served, caplog):
    _, handlers, base = served
    body = handoff_body()
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient() as http:
            response = await http.post(base + "/streamcore/connect-to-qconnect", content=body)

    assert response.status_code == 200 and response.json() == {}
    assert handlers.bodies == [body]
    assert_no_secrets(caplog.text)


@pytest.mark.asyncio
async def test_other_routes_and_methods_are_not_found(served):
    _, handlers, base = served
    async with httpx.AsyncClient() as http:
        unknown = await http.get(base + "/api/unpair")
        wrong_method = await http.get(base + "/streamcore/connect-to-qconnect")

    assert unknown.status_code == 404
    assert wrong_method.status_code == 404
    assert handlers.bodies == []


@pytest.mark.asyncio
async def test_an_oversized_declared_body_is_refused_unread(served):
    receiver, handlers, _ = served
    request = (
        b"POST /streamcore/connect-to-qconnect HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 70000\r\n\r\n"
    )

    response = await _raw(receiver.port, request)

    assert response.startswith(b"HTTP/1.1 413")
    assert handlers.bodies == []


@pytest.mark.asyncio
async def test_an_oversized_chunked_body_is_refused(served):
    receiver, handlers, _ = served
    chunk = b"a" * 40000
    request = (
        b"POST /streamcore/connect-to-qconnect HTTP/1.1\r\nHost: x\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        + (b"%x\r\n" % len(chunk) + chunk + b"\r\n") * 2
        + b"0\r\n\r\n"
    )

    response = await _raw(receiver.port, request)

    assert response.startswith(b"HTTP/1.1 413")
    assert handlers.bodies == []


@pytest.mark.asyncio
async def test_malformed_http_is_a_bad_request(served):
    receiver, _, _ = served

    response = await _raw(receiver.port, b"NONSENSE\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 400")


@pytest.mark.asyncio
async def test_a_client_that_stalls_is_cut_off(served, monkeypatch):
    receiver, _, _ = served
    monkeypatch.setattr(receiver_module, "REQUEST_DEADLINE_S", 0.2)

    response = await _raw(receiver.port, b"POST /streamcore/connect-to-qconnect HTTP/1.1\r\n")

    assert response.startswith(b"HTTP/1.1 408")


@pytest.mark.asyncio
async def test_a_failing_handler_answers_500_without_details():
    receiver = HandoffReceiver(_Handlers(fail=True), host="127.0.0.1", port=0)
    await receiver.start()
    try:
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"http://127.0.0.1:{receiver.port}/streamcore/connect-to-qconnect",
                content=handoff_body(),
            )
    finally:
        await receiver.stop()

    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}


@pytest.mark.asyncio
async def test_a_handoff_that_stops_the_endpoint_is_still_answered():
    """Linking an account closes the endpoint while the app awaits the handoff's answer."""
    stopping = []

    class _Linking(_Handlers):
        async def handoff(self, body):
            stopping.append(asyncio.create_task(receiver.stop()))
            # Long enough for a stop that does not wait to close this connection.
            await asyncio.sleep(0.05)
            return await super().handoff(body)

    receiver = HandoffReceiver(_Linking(), host="127.0.0.1", port=0)
    await receiver.start()
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"http://127.0.0.1:{receiver.port}/streamcore/connect-to-qconnect",
            content=handoff_body(),
        )
    await stopping[0]

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stop_closes_the_port():
    receiver = HandoffReceiver(_Handlers(), host="127.0.0.1", port=0)
    await receiver.start()
    port = receiver.port
    await receiver.stop()

    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_a_taken_port_fails_start():
    first = HandoffReceiver(_Handlers(), host="127.0.0.1", port=0)
    await first.start()
    try:
        with pytest.raises(OSError):
            await HandoffReceiver(_Handlers(), host="127.0.0.1", port=first.port).start()
    finally:
        await first.stop()
