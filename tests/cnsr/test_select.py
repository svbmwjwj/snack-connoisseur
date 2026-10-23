"""Tests for ranking validated SNI candidates and the shared backups.json format."""

from __future__ import annotations

import json

import pytest

from cnsr.errors import CnsrError
from cnsr.scan.select import Candidate, backups_json, choose, parse_backups, parse_validation


def test_choose_sorts_and_caps():
    cands = [Candidate(f"d{i}.example.com", "198.51.100.7", 100 - i) for i in range(15)]
    cur, backups = choose(cands)
    assert cur.latency_ms == 86 and len(backups) == 10 and backups[0].latency_ms == 87
    assert [b.latency_ms for b in backups] == list(range(87, 97))


def test_choose_empty_stops():
    with pytest.raises(CnsrError, match="不会盲选"):
        choose([])


def test_choose_message_is_exact():
    with pytest.raises(CnsrError) as raised:
        choose([])
    assert str(raised.value) == "没有候选通过节点验证，已停止，不会盲选"


def test_choose_single_candidate_has_no_backups():
    only = Candidate("a.example.com", "198.51.100.7", 50)
    assert choose([only]) == (only, [])


def test_choose_keeps_input_order_for_equal_latency_and_does_not_touch_the_input():
    cands = [
        Candidate("slow.example.com", "198.51.100.8", 90),
        Candidate("first.example.com", "198.51.100.9", 40),
        Candidate("second.example.com", "198.51.100.10", 40),
    ]
    before = list(cands)
    cur, backups = choose(cands)
    assert [cur.domain, *(b.domain for b in backups)] == ["first.example.com", "second.example.com", "slow.example.com"]
    assert cands == before


def test_choose_honours_max_backups():
    cands = [Candidate(f"d{i}.example.com", "198.51.100.7", i) for i in range(5)]
    cur, backups = choose(cands, max_backups=2)
    assert cur.latency_ms == 0 and [b.latency_ms for b in backups] == [1, 2]
    assert choose(cands, max_backups=0)[1] == []


def test_parse_validation_ignores_bad_lines():
    assert parse_validation("a.example.com 198.51.100.7 120\ngarbage\n") == [Candidate("a.example.com", "198.51.100.7", 120)]


def test_parse_validation_reads_every_good_line_in_order():
    text = "a.example.com 198.51.100.7 120\n\nb.example.com 2001:db8::7 85\r\n  c.example.com   203.0.113.9   7  \n"
    assert parse_validation(text) == [
        Candidate("a.example.com", "198.51.100.7", 120),
        Candidate("b.example.com", "2001:db8::7", 85),
        Candidate("c.example.com", "203.0.113.9", 7),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "a.example.com 198.51.100.7",  # no latency
        "a.example.com 198.51.100.7 12 extra",  # too many fields
        "a.example.com 198.51.100.7 fast",  # latency not a number
        "a.example.com 198.51.100.7 -5",  # negative latency
        "a.example.com 198.51.100.7 1.5",  # not an integer
        "a.example.com not-an-ip 120",
        "a.example.com 198.51.100.300 120",
        "bad/name 198.51.100.7 120",
        "curl: (6) Could not resolve",
    ],
)
def test_parse_validation_rejects_malformed_lines(line):
    assert parse_validation(line + "\n") == []


def test_parse_validation_of_nothing_is_empty():
    assert parse_validation("") == []


BACKUPS = [Candidate("a.example.com", "198.51.100.7", 87), Candidate("b.example.com", "203.0.113.5", 92)]


def test_backups_json_is_an_array_of_the_three_fields_with_a_trailing_newline():
    text = backups_json(BACKUPS)
    assert text.endswith("\n")
    assert json.loads(text) == [
        {"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": 87},
        {"domain": "b.example.com", "ip": "203.0.113.5", "latency_ms": 92},
    ]


def test_backups_round_trip():
    assert parse_backups(backups_json(BACKUPS)) == BACKUPS
    assert parse_backups(backups_json([])) == []


def test_parse_backups_tolerates_bom_crlf_and_extra_keys():
    text = '\ufeff[\r\n  {"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": 87, "note": "x"}\r\n]\r\n'
    assert parse_backups(text) == [BACKUPS[0]]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '[{"domain": "a.example.com"',  # truncated
        '{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": 1}',  # object, not an array
        "[1]",
        '[{"ip": "198.51.100.7", "latency_ms": 1}]',
        '[{"domain": "a.example.com", "latency_ms": 1}]',
        '[{"domain": "a.example.com", "ip": "198.51.100.7"}]',
        '[{"domain": "", "ip": "198.51.100.7", "latency_ms": 1}]',
        '[{"domain": "a.example.com", "ip": "nope", "latency_ms": 1}]',
        '[{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": "1"}]',
        '[{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": 1.5}]',
        '[{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": true}]',
        '[{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": -1}]',
        '[{"domain": 7, "ip": "198.51.100.7", "latency_ms": 1}]',
    ],
)
def test_parse_backups_rejects_bad_input_with_a_chinese_error(text):
    with pytest.raises(CnsrError, match="备用"):
        parse_backups(text)


def test_parse_backups_names_the_bad_entry():
    text = '[{"domain": "a.example.com", "ip": "198.51.100.7", "latency_ms": 1}, {"domain": "b.example.com"}]'
    with pytest.raises(CnsrError, match="第 2 项"):
        parse_backups(text)
