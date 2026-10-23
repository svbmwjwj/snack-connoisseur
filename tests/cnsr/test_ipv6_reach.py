"""Testing a node's IPv6 443 from this computer, with public addresses as a check of the route."""

from __future__ import annotations

from cnsr.scan.ipv6_reach import REFERENCES, Ipv6Reach, probe_ipv6

NODE = "2001:db8::20"


def connector(answers: dict[str, int | OSError]):
    tried: list[str] = []

    def connect(host: str) -> int:
        tried.append(host)
        answer = answers.get(host, OSError(65, "No route to host"))
        if isinstance(answer, OSError):
            raise answer
        return answer

    return connect, tried


def test_a_node_that_answers_passes_without_trying_the_references():
    connect, tried = connector({NODE: 31})
    assert probe_ipv6(NODE, connect=connect) == Ipv6Reach(ok=True, ms=31, error=None)
    assert tried == [NODE]


def test_a_silent_node_fails_when_a_reference_answers():
    connect, tried = connector({NODE: TimeoutError("timed out"), REFERENCES[-1]: 40})
    assert probe_ipv6(NODE, connect=connect) == Ipv6Reach(ok=False, ms=None, error="连接超时")
    assert tried == [NODE, *REFERENCES]


def test_a_refused_connection_names_the_reason():
    connect, _ = connector({NODE: ConnectionRefusedError(61, "Connection refused"), REFERENCES[0]: 40})
    assert probe_ipv6(NODE, connect=connect).error == "连接被拒绝"


def test_without_any_ipv6_route_the_node_is_not_judged():
    connect, tried = connector({})
    result = probe_ipv6(NODE, connect=connect)
    assert result.tested is False and result.ok is False and result.error
    assert tried == [NODE, *REFERENCES]


def test_in_mainland_china_a_domestic_reference_proves_ipv6_works():
    # 2026-10-19: from a mainland phone hotspot both nodes answered over IPv6, but Cloudflare's
    # and Google's public DNS on 443 timed out; Alibaba's public DNS serves DoH on 443 there.
    assert {"2400:3200::1", "2400:3200:baba::1"} <= set(REFERENCES)
    blocked = {ref: TimeoutError("timed out") for ref in REFERENCES if not ref.startswith("2400:3200")}
    connect, _ = connector({NODE: TimeoutError("timed out"), **blocked, "2400:3200::1": 35})
    result = probe_ipv6(NODE, connect)
    assert result.tested and not result.ok
