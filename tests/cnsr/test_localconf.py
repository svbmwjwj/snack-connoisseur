import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cnsr import localconf, paths
from cnsr.errors import CnsrError


def test_config_dir_windows():
    assert paths.config_dir({"APPDATA": r"C:\Users\a\AppData\Roaming"}, "win32", Path("C:/Users/a")) == Path(r"C:\Users\a\AppData\Roaming") / "cnsr"


def test_config_dir_windows_without_appdata_falls_back_to_home():
    assert paths.config_dir({}, "win32", Path("/h")) == Path("/h/AppData/Roaming/cnsr")


def test_config_dir_respects_xdg():
    assert paths.config_dir({"XDG_CONFIG_HOME": "/x"}, "linux", Path("/home/a")) == Path("/x/cnsr")


def test_config_dir_ignores_empty_xdg():
    assert paths.config_dir({"XDG_CONFIG_HOME": ""}, "linux", Path("/home/a")) == Path("/home/a/.config/cnsr")


def test_config_dir_default_posix():
    assert paths.config_dir({}, "darwin", Path("/Users/a")) == Path("/Users/a/.config/cnsr")


def test_ssh_paths_live_under_home_ssh():
    assert paths.ssh_config_path(Path("/h")) == Path("/h/.ssh/config")
    assert paths.pub_dir(Path("/h")) == Path("/h/.ssh/pub")


def test_keys_are_in_documented_order():
    assert localconf.KEYS == ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CNSR_SSH_AGENT")


def test_load_tolerates_crlf_bom_quotes(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_bytes(b'\xef\xbb\xbfCLOUDFLARE_API_TOKEN="abc"\r\n# c\r\n\r\nCLOUDFLARE_ZONE=example.com\r\n')
    assert localconf.load(p) == {"CLOUDFLARE_API_TOKEN": "abc", "CLOUDFLARE_ZONE": "example.com"}


def test_load_strips_matching_single_quotes_only(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("A='x y'\nB=\"half\nC='mixed\"\nD=\n", encoding="utf-8")
    assert localconf.load(p) == {"A": "x y", "B": '"half', "C": "'mixed\"", "D": ""}


def test_load_ignores_lines_without_equals(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("garbage\nCLOUDFLARE_ZONE=example.com\n", encoding="utf-8")
    assert localconf.load(p) == {"CLOUDFLARE_ZONE": "example.com"}


def test_load_missing_file_is_empty(tmp_path):
    assert localconf.load(tmp_path / "none.env") == {}


@pytest.mark.skipif(sys.platform == "win32", reason="posix permissions")
def test_save_writes_lf_and_mode_600(tmp_path):
    p = tmp_path / "secrets.env"
    localconf.save(p, {"CLOUDFLARE_ZONE": "example.com"})
    assert b"\r" not in p.read_bytes()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_save_on_windows_restricts_with_icacls(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("USERNAME", "alice")
    localconf.save(tmp_path / "s.env", {"CLOUDFLARE_ZONE": "example.com"}, platform="win32", run=lambda a, **k: calls.append(a))
    assert calls == [["icacls", str(tmp_path / "s.env"), "/inheritance:r", "/grant:r", "alice:F"]]


def test_save_creates_missing_parent_dir(tmp_path):
    p = tmp_path / "a" / "b" / "secrets.env"
    localconf.save(p, {"CLOUDFLARE_ZONE": "example.com"})
    assert localconf.load(p) == {"CLOUDFLARE_ZONE": "example.com"}


def test_save_orders_known_keys_and_keeps_unknown(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("EXTRA=keep-me\nCLOUDFLARE_ZONE=old.example.com\n", encoding="utf-8")
    values = localconf.load(p)
    values["AWS_ACCESS_KEY_ID"] = "AKIAEXAMPLE"
    values["CLOUDFLARE_API_TOKEN"] = "tok"
    localconf.save(p, values)
    assert p.read_text(encoding="utf-8") == (
        "CLOUDFLARE_API_TOKEN=tok\n"
        "CLOUDFLARE_ZONE=old.example.com\n"
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
        "EXTRA=keep-me\n"
    )


def test_save_replaces_file_without_leaving_temp_files(tmp_path):
    p = tmp_path / "secrets.env"
    localconf.save(p, {"CLOUDFLARE_ZONE": "one.example.com"})
    localconf.save(p, {"CLOUDFLARE_ZONE": "two.example.com"})
    assert localconf.load(p) == {"CLOUDFLARE_ZONE": "two.example.com"}
    assert [x.name for x in tmp_path.iterdir()] == ["secrets.env"]


def test_save_roundtrip_through_load(tmp_path):
    p = tmp_path / "secrets.env"
    values = {"CLOUDFLARE_API_TOKEN": "tok", "CNSR_SSH_AGENT": "/a b/agent.sock", "OTHER": "1"}
    localconf.save(p, values)
    assert localconf.load(p) == values


def test_save_rejects_newline_in_value(tmp_path):
    p = tmp_path / "secrets.env"
    with pytest.raises(CnsrError):
        localconf.save(p, {"CLOUDFLARE_API_TOKEN": "abc\nCLOUDFLARE_ZONE=evil.example.com"})
    assert not p.exists()


def test_save_on_windows_reports_icacls_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "alice")

    def failing_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    with pytest.raises(CnsrError):
        localconf.save(tmp_path / "s.env", {"CLOUDFLARE_ZONE": "example.com"}, platform="win32", run=failing_run)


def test_save_on_windows_requires_username(tmp_path, monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    p = tmp_path / "s.env"
    with pytest.raises(CnsrError):
        localconf.save(p, {"CLOUDFLARE_ZONE": "example.com"}, platform="win32", run=lambda a, **k: None)
    assert not p.exists()


def test_save_keeps_unknown_keys_missing_from_values(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("EXTRA=keep-me\nCLOUDFLARE_ZONE=old.example.com\n", encoding="utf-8")
    localconf.save(p, {"CLOUDFLARE_API_TOKEN": "tok"})
    assert p.read_text(encoding="utf-8") == "CLOUDFLARE_API_TOKEN=tok\nEXTRA=keep-me\n"


def test_save_lets_values_override_retained_unknown_key(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("EXTRA=old\n", encoding="utf-8")
    localconf.save(p, {"EXTRA": "new"})
    assert localconf.load(p) == {"EXTRA": "new"}


TRICKY_VALUES = ["'q'", '"abc"', " pad ", " a ", "\ta", '""', "''", '"', "'", '"half', "'mixed\"", "a=b", "x # y", "/a b/agent.sock", ""]


@pytest.mark.parametrize("value", TRICKY_VALUES)
def test_save_then_load_roundtrips_exact_values(tmp_path, value):
    p = tmp_path / "secrets.env"
    values = {"CLOUDFLARE_API_TOKEN": value, "EXTRA": value}
    localconf.save(p, values)
    assert localconf.load(p) == values


def test_retained_unknown_keys_survive_save_unchanged(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("EXTRA=\"'q'\"\nPAD=\" a \"\nQ2='\"abc\"'\n", encoding="utf-8")
    before = localconf.load(p)
    assert before == {"EXTRA": "'q'", "PAD": " a ", "Q2": '"abc"'}
    localconf.save(p, {"CLOUDFLARE_ZONE": "example.com"})
    assert localconf.load(p) == {"CLOUDFLARE_ZONE": "example.com", **before}
    localconf.save(p, {"CLOUDFLARE_ZONE": "example.com"})
    assert localconf.load(p) == {"CLOUDFLARE_ZONE": "example.com", **before}


def test_load_names_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_bytes(b"CLOUDFLARE_ZONE=example.com\nCNSR_SSH_AGENT=/home/\xff\xfe\n")
    with pytest.raises(CnsrError) as err:
        localconf.load(path)
    assert str(path) in str(err.value) and "UTF-8" in str(err.value)


@pytest.mark.skipif(sys.platform == "win32", reason="posix permissions")
def test_restrict_to_user_sets_mode_600(tmp_path):
    p = tmp_path / "f.log"
    p.write_text("x", encoding="utf-8")
    p.chmod(0o644)
    localconf.restrict_to_user(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_restrict_to_user_on_windows_uses_icacls(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("USERNAME", "alice")
    localconf.restrict_to_user(tmp_path / "f.log", platform="win32", run=lambda a, **k: calls.append(a))
    assert calls == [["icacls", str(tmp_path / "f.log"), "/inheritance:r", "/grant:r", "alice:F"]]


def test_load_registers_the_credentials(tmp_path, monkeypatch):
    from cnsr import runlog

    registered = []
    monkeypatch.setattr(runlog, "secret", registered.append)
    path = tmp_path / "secrets.env"
    path.write_text(
        "CLOUDFLARE_API_TOKEN=cf-token-123456\nCLOUDFLARE_ZONE=example.com\n"
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE12345\nAWS_SECRET_ACCESS_KEY=aws-secret-123456\n",
        encoding="utf-8",
    )
    localconf.load(path)
    assert sorted(registered) == ["AKIAEXAMPLE12345", "aws-secret-123456", "cf-token-123456"]
    assert "example.com" not in registered
