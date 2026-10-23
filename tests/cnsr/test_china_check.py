"""china-check.sh: Globalping's mainland probes try a TLS handshake with a candidate's SNI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "cnsr" / "assets" / "node" / "china-check.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

# Answers "POST <api>/measurements" with fake/create.json and "GET .../<id>" with the next of
# fake/poll-<n>.json (the last repeating); records every call in fake/calls.jsonl.
FAKE_CURL = r"""#!{python}
import json, os, sys
fake = os.environ["FAKE_DIR"]
args = sys.argv[1:]
url = args[-1]
body = args[args.index("-d") + 1] if "-d" in args else None
with open(os.path.join(fake, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"url": url, "body": body, "args": args}) + "\n")
if os.path.exists(os.path.join(fake, "fail")):
    sys.exit(7)
if body is not None:
    sys.stdout.write(open(os.path.join(fake, "create.json")).read())
    sys.exit(0)
counter = os.path.join(fake, "polls")
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
polls = sorted(p for p in os.listdir(fake) if p.startswith("poll-"))
sys.stdout.write(open(os.path.join(fake, polls[min(n, len(polls) - 1)])).read())
"""

TLS = {"protocol": "TLSv1.3", "authorized": True, "subject": {"CN": "a.example.com"}}
EXPIRED = {"protocol": "TLSv1.3", "authorized": False, "error": "certificate has expired"}


def probe(tls=None, raw="", status="finished") -> dict:
    return {"probe": {"city": "Beijing"}, "result": {"status": status, "tls": tls, "rawOutput": raw}}


def measurement(*results: dict, status: str = "finished") -> dict:
    return {"id": "m1", "status": status, "results": list(results)}


@pytest.fixture
def check(tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed on this machine")
    fake, bin_dir = tmp_path / "fake", tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL.replace("{python}", sys.executable))
    curl.chmod(0o755)
    for tool in ("jq", "sleep", "date", "cat"):
        (bin_dir / tool).symlink_to(shutil.which(tool))

    def run(*polls: dict, create: dict | None = None, args=("a.example.com", "203.0.113.10"), poll_seconds="0"):
        (fake / "create.json").write_text(json.dumps(create if create is not None else {"id": "m1", "probesCount": 3}))
        for n, poll in enumerate(polls):
            (fake / f"poll-{n:02d}.json").write_text(json.dumps(poll))
        env = {"PATH": str(bin_dir), "FAKE_DIR": str(fake), "CNSR_CHINA_POLL_SECONDS": poll_seconds}
        done = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(line) for line in (fake / "calls.jsonl").read_text().splitlines()] if (fake / "calls.jsonl").exists() else []
        return done, calls

    run.fake = fake
    return run


def test_handshakes_from_most_probes_pass(check):
    done, calls = check(measurement(probe(TLS), probe(TLS), probe(raw="read ECONNRESET", status="failed")))
    assert (done.returncode, done.stdout) == (0, "pass\n")
    sent = json.loads(calls[0]["body"])
    assert sent["target"] == "203.0.113.10" and sent["measurementOptions"]["request"]["host"] == "a.example.com"
    assert sent["measurementOptions"]["protocol"] == "HTTPS" and sent["locations"] == [{"country": "CN", "limit": 3}]
    assert calls[1]["url"].endswith("/measurements/m1")


def test_resets_from_most_probes_mean_blocked(check):
    reset = probe(raw="read ECONNRESET", status="failed")
    done, _ = check(measurement(reset, reset, probe(TLS)))
    assert done.stdout == "blocked\n"


# What the current Globalping probe says (src/command/handlers/http/undici.ts).
TLS_TIMEOUT = "Request timed out during the TLS handshake."
TCP_TIMEOUT = "Request timed out while establishing the TCP connection."


def test_a_tls_handshake_cut_off_counts_as_blocked(check):
    cut = [
        probe(raw=TLS_TIMEOUT, status="failed"),
        probe(raw="Client network socket disconnected before secure TLS connection was established", status="failed"),
        probe(raw="socket hang up", status="failed"),
    ]
    done, _ = check(measurement(*cut))
    assert done.stdout == "blocked\n"


def test_a_neighbour_china_cannot_reach_at_all_is_not_an_sni_block(check):
    # Only the node connects to the neighbour; a TCP failure from China says nothing about the SNI.
    tcp = probe(raw=TCP_TIMEOUT, status="failed")
    refused = probe(raw="connect ECONNREFUSED 203.0.113.10:443", status="failed")
    done, _ = check(measurement(tcp, tcp, refused))
    assert done.stdout == "unknown\n"


def test_a_measurement_still_running_is_polled_until_it_finishes(check):
    running = measurement(probe(TLS), status="in-progress")
    done, calls = check(running, running, measurement(probe(TLS), probe(TLS), probe(TLS)))
    assert done.stdout == "pass\n" and len(calls) == 4


@pytest.mark.parametrize(
    "poll",
    [
        measurement(probe(TLS), probe(raw="read ECONNRESET", status="failed")),  # one each: no majority
        measurement(probe(TLS)),  # too few probes
        measurement(probe(raw="getaddrinfo ENOTFOUND", status="failed"), probe(TLS), probe(raw="x", status="failed")),
    ],
)
def test_no_clear_answer_is_unknown(check, poll):
    done, _ = check(poll)
    assert (done.returncode, done.stdout) == (0, "unknown\n")


def test_a_refused_request_is_unknown(check):
    done, calls = check(create={"error": {"type": "too_many_requests"}})
    assert done.stdout == "unknown\n" and len(calls) == 1


def test_an_unreachable_api_is_unknown(check):
    (check.fake / "fail").touch()
    done, _ = check()
    assert (done.returncode, done.stdout) == (0, "unknown\n")


def test_a_measurement_that_never_finishes_is_unknown(check):
    done, calls = check(measurement(status="in-progress"))
    assert done.stdout == "unknown\n" and len(calls) > 2


def test_an_odd_measurement_id_is_not_used_in_a_url(check):
    done, calls = check(create={"id": "../x?y"})
    assert done.stdout == "unknown\n" and len(calls) == 1


def test_bad_arguments_are_refused(check):
    done, calls = check(args=("a.example.com",))
    assert done.returncode == 2 and calls == []


def test_a_handshake_with_a_bad_certificate_still_passes(check):
    # The probe does not reject certificates; the node checks them itself.
    done, _ = check(measurement(probe(EXPIRED), probe(EXPIRED), probe(TLS)))
    assert done.stdout == "pass\n"


def test_the_request_does_not_name_cnsr(check):
    _, calls = check(measurement(probe(TLS), probe(TLS), probe(TLS)))
    for call in calls:
        assert "-A" not in call["args"] and not any("cnsr" in arg.lower() for arg in call["args"])


def test_a_time_limit_caps_every_request(check):
    _, calls = check(measurement(probe(TLS), probe(TLS), probe(TLS)), args=("a.example.com", "203.0.113.10", "5"))
    assert calls and all(int(call["args"][call["args"].index("--max-time") + 1]) <= 5 for call in calls)


def test_a_time_limit_ends_the_polling(check):
    started = time.monotonic()
    done, calls = check(measurement(status="in-progress"), args=("a.example.com", "203.0.113.10", "2"), poll_seconds="1")
    assert done.stdout == "unknown\n"
    assert time.monotonic() - started < 5 and len(calls) < 4  # 8 polls without the limit


def test_a_bad_time_limit_is_refused(check):
    done, calls = check(args=("a.example.com", "203.0.113.10", "soon"))
    assert done.returncode == 2 and calls == []
