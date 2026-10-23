import socket

from cnsr.scan import tcp_reach


def test_an_open_port_and_a_closed_one():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert tcp_reach.connect("127.0.0.1", port) == "open"
    assert tcp_reach.connect("127.0.0.1", port) == "refused"


def test_nothing_answering_is_a_timeout(monkeypatch):
    def silent(address, timeout):
        raise TimeoutError

    monkeypatch.setattr(tcp_reach.socket, "create_connection", silent)
    assert tcp_reach.connect("192.0.2.1", 443) == "timeout"
