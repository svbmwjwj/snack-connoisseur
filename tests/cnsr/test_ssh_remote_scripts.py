"""Tests for streaming bundled scripts and writing files on a server."""

from __future__ import annotations

import pytest
from ssh_fakes import ADMIN, CONFIG, T, FakeSsh

from cnsr.errors import CnsrError
from cnsr.ssh import remote
from cnsr.ssh.remote import Remote


@pytest.fixture
def fake_assets(tmp_path, monkeypatch):
    """Point the node script reader at a temporary directory and return it."""
    directory = tmp_path / "node"
    directory.mkdir()
    monkeypatch.setattr(remote.resources, "files", lambda package: directory)
    return directory


# --- scripts and files -------------------------------------------------------------------


def test_run_script_streams_asset(fake_assets):
    text = "#!/usr/bin/env bash\nset -eu\necho \"$1\"\n"
    (fake_assets / "setup.sh").write_bytes(text.encode())
    fake = FakeSsh(stdout=b"done")
    assert Remote(ADMIN, CONFIG, run=fake).run_script("setup.sh", ["23456"]) == "done"
    assert fake.kwargs["input"] == text.encode()
    assert fake.args[-1] == "sudo -n timeout -k 10 105 bash -s -- 23456"


@pytest.mark.parametrize(
    ("name", "limit"),
    [("install-docker.sh", 600), ("baseline-check.sh", 420), ("validate-candidates.sh", 420), ("preflight.sh", 150)],
)
def test_each_script_has_its_own_time_limit(fake_assets, name, limit):
    (fake_assets / name).write_text("true\n")
    fake = FakeSsh()
    Remote(ADMIN, CONFIG, run=fake).run_script(name, [])
    assert fake.kwargs["timeout"] == limit


def test_every_bundled_script_has_a_time_limit():
    bundled = {path.name for path in remote.resources.files("cnsr.assets.node").iterdir() if path.name.endswith(".sh")}
    run_by_add = bundled - {"cnsr-rotate.sh", "rotate-control.sh", "rotate-switch.sh", "china-check.sh"}  # the service's
    assert run_by_add <= set(remote.SCRIPT_TIMEOUTS)


def test_a_script_that_runs_too_long_is_named(fake_assets):
    (fake_assets / "preflight.sh").write_text("true\n")
    fake = FakeSsh(raises=remote.subprocess.TimeoutExpired(["ssh"], 150))
    with pytest.raises(CnsrError) as caught:
        Remote(ADMIN, CONFIG, run=fake).run_script("preflight.sh", ["22"])
    assert "服务器脚本 preflight.sh 超过 150 秒" in str(caught.value)


def test_the_server_stops_a_script_before_this_computer_gives_up(fake_assets):
    # A killed ssh leaves bash running on the server; the server's own limit ends it first.
    (fake_assets / "harden-stage.sh").write_text("true\n")
    fake = FakeSsh()
    Remote(ADMIN, CONFIG, run=fake).run_script("harden-stage.sh", ["23456"])
    assert fake.args[-1] == "sudo -n timeout -k 10 405 bash -s -- 23456" and fake.kwargs["timeout"] == 420


@pytest.mark.parametrize("code", [124, 137])
def test_a_script_the_server_stopped_is_named_like_a_local_timeout(fake_assets, code):
    (fake_assets / "harden-stage.sh").write_text("true\n")
    with pytest.raises(CnsrError) as caught:
        Remote(ADMIN, CONFIG, run=FakeSsh(returncode=code)).run_script("harden-stage.sh", [])
    assert "服务器脚本 harden-stage.sh 超过 420 秒" in str(caught.value)


def test_run_script_sudo_rules_and_awkward_args(fake_assets):
    (fake_assets / "setup.sh").write_text("true\n")
    as_admin, as_root = FakeSsh(), FakeSsh()
    Remote(ADMIN, CONFIG, run=as_admin).run_script("setup.sh", ["a b", "-x", ""], sudo=False)
    Remote(T, CONFIG, run=as_root).run_script("setup.sh", [])
    assert as_admin.args[-1] == "timeout -k 10 105 bash -s -- 'a b' -x ''"
    assert as_root.args[-1] == "timeout -k 10 105 bash -s --"


def test_run_script_sends_lf_without_bom(fake_assets):
    (fake_assets / "setup.sh").write_bytes(b"\xef\xbb\xbfecho a\r\necho b\r\n")
    fake = FakeSsh()
    Remote(T, CONFIG, run=fake).run_script("setup.sh", [])
    assert fake.kwargs["input"] == b"echo a\necho b\n"


@pytest.mark.parametrize("name", ["", ".", "..", "../secrets.sh", "sub/x.sh", "sub\\x.sh", "missing.sh"])
def test_run_script_refuses_names_it_cannot_serve(fake_assets, name):
    (fake_assets.parent / "secrets.sh").write_text("true\n")  # reachable only through "../"
    (fake_assets / "sub").mkdir()
    (fake_assets / "sub" / "x.sh").write_text("true\n")
    fake = FakeSsh()
    with pytest.raises(CnsrError):
        Remote(T, CONFIG, run=fake).run_script(name, [])
    assert fake.calls == []


def test_run_script_reports_a_script_missing_from_the_package(monkeypatch):
    fake = FakeSsh()
    with pytest.raises(CnsrError, match="no-such-script.sh"):
        Remote(T, CONFIG, run=fake).run_script("no-such-script.sh", [])  # the real package

    def no_package(package):
        raise ModuleNotFoundError(package)

    monkeypatch.setattr(remote.resources, "files", no_package)
    with pytest.raises(CnsrError, match="setup.sh"):
        Remote(T, CONFIG, run=fake).run_script("setup.sh", [])
    assert fake.calls == []


def test_put_text_installs_from_stdin_with_sudo_unless_root():
    fake = FakeSsh()
    Remote(ADMIN, CONFIG, run=fake).put_text("/etc/cnsr/env", "TOKEN=x\n")
    assert fake.args[-1] == "sudo -n install -m 600 /dev/stdin /etc/cnsr/env"
    assert fake.kwargs["input"] == b"TOKEN=x\n"
    Remote(T, CONFIG, run=fake).put_text("/opt/cnsr/compose.yml", "a: 1\n", mode="0644")
    assert fake.args[-1] == "install -m 0644 /dev/stdin /opt/cnsr/compose.yml"


def test_put_text_content_arrives_byte_exact():
    content = "line one\r\né中文\n\nno newline at the end"
    fake = FakeSsh()
    Remote(T, CONFIG, run=fake).put_text("/etc/x", content)
    assert fake.kwargs["input"] == content.encode("utf-8")
    assert not fake.kwargs.get("text") and not fake.kwargs.get("universal_newlines")


@pytest.mark.parametrize(
    "path,mode", [("/etc/x", m) for m in ("", "rw", "-t", "9", "6 0", "0o600", "77777")] + [("", "600"), ("-o", "600")]
)
def test_put_text_refuses_a_mode_or_path_install_would_misread(path, mode):
    fake = FakeSsh()
    with pytest.raises(CnsrError):
        Remote(T, CONFIG, run=fake).put_text(path, "a", mode=mode)
    assert fake.calls == []
