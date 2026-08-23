import os
import socket

import pytest

from nukemcp import connection
from fake_nuke_listener import FakeNukeListener


@pytest.fixture
def fake_listener():
    listener = FakeNukeListener(handlers={
        "echo": lambda params: params,
        "boom": lambda params: (_ for _ in ()).throw(ValueError("kaboom")),
    })
    listener.start()
    old_port = os.environ.get("NUKEMCP_PORT")
    os.environ["NUKEMCP_PORT"] = str(listener.port)
    try:
        yield listener
    finally:
        listener.stop()
        if old_port is None:
            os.environ.pop("NUKEMCP_PORT", None)
        else:
            os.environ["NUKEMCP_PORT"] = old_port


def test_send_request_success(fake_listener):
    result = connection.send_request("echo", {"hello": "world"})
    assert result == {"hello": "world"}


def test_send_request_tool_error(fake_listener):
    with pytest.raises(connection.NukeToolError) as exc_info:
        connection.send_request("boom", {})
    assert "kaboom" in str(exc_info.value)
    assert exc_info.value.error_type == "ValueError"


def test_send_request_unknown_tool(fake_listener):
    with pytest.raises(connection.NukeToolError) as exc_info:
        connection.send_request("does_not_exist", {})
    assert exc_info.value.error_type == "UnknownToolError"


def _closed_port():
    """A port nothing is listening on, by construction.

    Binding and immediately closing leaves the number free. A hardcoded number
    carries an assumption the test cannot check: port 1, used here before,
    accepts connections on at least one macOS machine, so the test failed with
    "Invalid response" -- the client had connected and read nothing -- rather
    than the refusal it was written to check.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_connection_refused_when_nothing_listening():
    old_port = os.environ.get("NUKEMCP_PORT")
    os.environ["NUKEMCP_PORT"] = str(_closed_port())
    try:
        with pytest.raises(connection.NukeConnectionError) as exc_info:
            connection.send_request("get_script_info", {})
        assert "Could not connect to Nuke" in str(exc_info.value)
    finally:
        if old_port is None:
            os.environ.pop("NUKEMCP_PORT", None)
        else:
            os.environ["NUKEMCP_PORT"] = old_port
