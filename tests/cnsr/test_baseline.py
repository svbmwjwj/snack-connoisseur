"""Tests for reading `sshd -T` and checking the SSH login baseline."""

from __future__ import annotations

import pytest

from cnsr.server.baseline import LOGINS, connection_spec, findings, parse_contexts, parse_sshd_t, per_login

SSHD_T = """port 22
port 23456
addressfamily any
listenaddress [::]:22
listenaddress 0.0.0.0:22
permitrootlogin without-password
passwordauthentication no
kbdinteractiveauthentication no
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2
"""
GOOD = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": "no"}


def test_parse_sshd_t_reads_each_setting():
    sshd = parse_sshd_t(SSHD_T)
    assert sshd["permitrootlogin"] == "without-password"
    assert sshd["passwordauthentication"] == "no"
    assert sshd["authorizedkeysfile"] == ".ssh/authorized_keys .ssh/authorized_keys2"


def test_parse_sshd_t_joins_repeated_settings_in_order():
    sshd = parse_sshd_t(SSHD_T)
    assert sshd["port"] == "22 23456"
    assert sshd["listenaddress"] == "[::]:22 0.0.0.0:22"


def test_parse_sshd_t_tolerates_crlf_blank_lines_and_upper_case():
    assert parse_sshd_t("\r\nPort 22\r\n\r\n  PasswordAuthentication   no \r\n") == {
        "port": "22",
        "passwordauthentication": "no",
    }


def test_parse_sshd_t_of_nothing_is_empty():
    assert parse_sshd_t("") == {}


def test_baseline_ok_with_prohibit_password():
    assert findings({"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": "prohibit-password"}) == []


@pytest.mark.parametrize("root", ["no", "prohibit-password", "without-password", "forced-commands-only"])
def test_baseline_accepts_every_root_setting_except_yes(root):
    assert findings({**GOOD, "permitrootlogin": root}) == []


def test_baseline_flags_password_login():
    assert findings({"passwordauthentication": "yes", "kbdinteractiveauthentication": "no", "permitrootlogin": "no"}) == ["SSH 允许密码登录"]


def test_baseline_flags_keyboard_interactive_and_root_password_login():
    sshd = {"passwordauthentication": "no", "kbdinteractiveauthentication": "yes", "permitrootlogin": "yes"}
    assert findings(sshd) == ["SSH 允许键盘交互登录", "SSH 允许 root 用密码登录"]


def test_baseline_fails_closed_when_login_settings_are_missing():
    assert findings({}) == ["SSH 允许密码登录", "SSH 允许键盘交互登录"]


def test_baseline_reads_real_sshd_t_output():
    assert findings(parse_sshd_t(SSHD_T)) == []
    assert findings(parse_sshd_t(SSHD_T.replace("passwordauthentication no", "passwordauthentication yes"))) == ["SSH 允许密码登录"]


# --- per login: Match blocks apply to some logins only ------------------------------------


def test_the_logins_checked_and_their_connection_spec():
    assert LOGINS == ("admin", "root")
    assert connection_spec("root") == "user=root,host=cnsr,addr=192.0.2.1"


def test_parse_contexts_splits_the_script_output_per_login():
    root = SSHD_T.replace("permitrootlogin without-password", "permitrootlogin yes")
    contexts = parse_contexts("cnsr-context admin\n" + SSHD_T + "cnsr-context root\n" + root)
    assert set(contexts) == {"admin", "root"}
    assert (contexts["admin"]["permitrootlogin"], contexts["root"]["permitrootlogin"]) == ("without-password", "yes")


def test_a_problem_of_one_login_only_names_it():
    assert per_login({"admin": GOOD, "root": {**GOOD, "permitrootlogin": "yes"}}) == ["SSH 允许 root 用密码登录（root 登录时）"]


def test_a_problem_every_login_has_is_listed_once():
    bad = {**GOOD, "passwordauthentication": "yes"}
    assert per_login({"admin": bad, "root": bad}) == ["SSH 允许密码登录"]


def test_a_login_without_settings_fails_closed():
    assert per_login({"admin": GOOD}) == ["SSH 允许密码登录（root 登录时）", "SSH 允许键盘交互登录（root 登录时）"]


def test_per_login_takes_another_check():
    assert per_login({"admin": {"port": "1"}, "root": {"port": "2"}}, lambda sshd: [f"port {sshd['port']}"]) == [
        "port 1（admin 登录时）", "port 2（root 登录时）",
    ]  # fmt: skip
