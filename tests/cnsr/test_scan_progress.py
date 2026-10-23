"""The neighbour scan reports how many addresses it has finished."""

from __future__ import annotations

import asyncio
import threading

import pytest

from cnsr.scan import probe
from cnsr.scan.probe import scan


@pytest.fixture
def no_network(monkeypatch):
    """Every handshake fails at once, after a delay that makes later addresses finish first."""

    async def refused(ip, port, server_hostname):
        await asyncio.sleep(0.02 - int(ip.rsplit(".", 1)[1]) * 0.001)
        raise OSError("refused")

    monkeypatch.setattr(probe, "_handshake", refused)


def test_progress_counts_every_finished_address(no_network):
    seen = []
    ips = [f"192.0.2.{n}" for n in range(1, 11)]
    threads = set()

    def progress(done, total):
        seen.append((done, total))
        threads.add(threading.get_ident())

    assert scan(ips, progress=progress, concurrency=3) == []
    assert seen == [(n, 10) for n in range(1, 11)]
    assert threads == {threading.get_ident()}  # called on the caller's thread


def test_no_progress_is_fine(no_network):
    assert scan(["192.0.2.1"]) == []


def test_a_failing_progress_callback_stops_the_scan(no_network):
    def progress(done, total):
        raise RuntimeError("display broke")

    with pytest.raises(RuntimeError, match="display broke"):
        scan(["192.0.2.1", "192.0.2.2"], progress=progress)
