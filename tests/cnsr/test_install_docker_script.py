"""Tests for install-docker.sh, run against a fake server (see xray_script_harness)."""

from __future__ import annotations

import sys

import pytest
from xray_script_harness import box, harden_tool  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

SCRIPT = "install-docker.sh"
KEY = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBFit2ioBEADhWpZ8\n-----END PGP PUBLIC KEY BLOCK-----"
APT = "apt-get -qq -o DPkg::Lock::Timeout=120"
CONF = "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
COMPOSE = "docker compose version"
PACKAGES = "docker-ce docker-ce-cli containerd.io docker-compose-plugin"
JQ = "dpkg-query -W -f=${Status} jq"
KEYRING = "/etc/apt/keyrings/docker.asc"
SOURCES = "/etc/apt/sources.list.d/docker.list"


def fresh(box, os_id: str = "debian", codename: str = "trixie") -> None:
    """A system without Docker: compose fails once, then works after the install."""
    lines = ['PRETTY_NAME="Test Linux"', f"ID={os_id}", 'VERSION_ID="1"']
    if codename:
        lines.append(f"VERSION_CODENAME={codename}")
    box.write("/etc/os-release", "\n".join(lines) + "\n")
    box.answer(COMPOSE, status="1 0")
    box.answer("dpkg --print-architecture", "amd64\n")
    box.answer(f"curl -fsSL --retry 3 --max-time 60 https://download.docker.com/linux/{os_id}/gpg", KEY + "\n")


def test_skips_everything_when_compose_and_jq_are_there(box):
    box.answer(JQ, "install ok installed")
    result = box.run(SCRIPT)
    assert result.status == 0 and result.stdout == ""
    assert result.calls == [JQ, COMPOSE]
    assert not box.path("/etc/apt").exists()


def test_installs_only_jq_when_docker_is_there(box):
    result = box.run(SCRIPT)
    assert result.status == 0, result.stderr
    assert result.calls == [JQ, COMPOSE, f"{APT} update", f"{APT} {CONF} install -y jq"]
    assert not box.path("/etc/apt/sources.list.d").exists()


def test_a_jq_that_cannot_be_installed_fails(box):
    box.answer(f"{APT} {CONF} install -y jq", status=100)
    assert box.run(SCRIPT).status == 100


def test_installs_from_the_official_repository(box):
    fresh(box)
    result = box.run(SCRIPT)
    assert result.status == 0, result.stderr
    assert result.calls == [
        JQ,
        COMPOSE,
        "dpkg --print-architecture",
        "dpkg-query -W -f=${Status} ca-certificates",
        "dpkg-query -W -f=${Status} curl",
        f"{APT} update",
        f"{APT} {CONF} install -y ca-certificates curl",
        "curl -fsSL --retry 3 --max-time 60 https://download.docker.com/linux/debian/gpg",
        f"{APT} update",
        f"{APT} {CONF} install -y {PACKAGES} jq",
        "systemctl enable --quiet --now docker",
        COMPOSE,
    ]
    assert box.read(KEYRING) == KEY + "\n" and box.mode(KEYRING) == 0o644
    line = "deb [arch=amd64 signed-by={root}{keyring}] https://download.docker.com/linux/debian trixie stable\n"
    assert box.read(SOURCES) == line.format(root=box.root, keyring=KEYRING) and box.mode(SOURCES) == 0o644
    assert box.mode("/etc/apt/keyrings") == 0o755
    assert box.leftovers() == [] and not result.stdin_leaked


def test_ubuntu_uses_the_ubuntu_repository_and_its_codename(box):
    fresh(box, "ubuntu", "noble")
    result = box.run(SCRIPT)
    assert result.status == 0, result.stderr
    assert "curl -fsSL --retry 3 --max-time 60 https://download.docker.com/linux/ubuntu/gpg" in result.calls
    assert box.read(SOURCES).endswith(" https://download.docker.com/linux/ubuntu noble stable\n")


def test_does_not_reinstall_the_prerequisites_that_are_there(box):
    fresh(box)
    for package in ("ca-certificates", "curl"):
        box.answer(f"dpkg-query -W -f=${{Status}} {package}", "install ok installed")
    result = box.run(SCRIPT)
    assert result.status == 0, result.stderr
    assert not any("install -y ca-certificates" in call for call in result.calls)
    assert result.calls.count(f"{APT} update") == 1


def test_installs_only_the_prerequisite_that_is_missing(box):
    fresh(box)
    box.answer("dpkg-query -W -f=${Status} ca-certificates", "install ok installed")
    result = box.run(SCRIPT)
    assert f"{APT} {CONF} install -y curl" in result.calls


def test_replaces_an_older_repository_file(box):
    fresh(box)
    box.write(SOURCES, "deb stale\n")
    box.write(KEYRING, "stale\n")
    assert box.run(SCRIPT).status == 0
    assert "stale" not in box.read(SOURCES) and box.read(KEYRING) == KEY + "\n"


@pytest.mark.parametrize("os_id", ["fedora", "alpine", ""])
def test_refuses_a_system_that_is_not_debian_or_ubuntu(box, os_id):
    fresh(box, os_id)
    result = box.run(SCRIPT)
    assert result.status == 1 and "不支持的系统" in result.stderr
    assert result.calls == [JQ, COMPOSE]
    assert not box.path("/etc/apt").exists()


def test_refuses_a_system_without_a_codename(box):
    fresh(box, codename="")
    result = box.run(SCRIPT)
    assert result.status == 1 and "VERSION_CODENAME" in result.stderr
    assert not box.path("/etc/apt").exists()


def test_refuses_when_os_release_is_missing(box):
    box.answer(COMPOSE, status=1)
    result = box.run(SCRIPT)
    assert result.status == 1 and "os-release" in result.stderr


def test_stops_when_the_key_cannot_be_downloaded(box):
    fresh(box)
    box.answer("curl -fsSL --retry 3 --max-time 60 https://download.docker.com/linux/debian/gpg", "", status=22)
    result = box.run(SCRIPT)
    assert result.status == 1 and "签名密钥" in result.stderr
    assert not box.path(KEYRING).exists() and not box.path(SOURCES).exists()
    assert not any(PACKAGES in call for call in result.calls)


def test_stops_when_the_download_is_not_a_pgp_key(box):
    fresh(box)
    box.answer("curl -fsSL --retry 3 --max-time 60 https://download.docker.com/linux/debian/gpg", "<html>blocked</html>\n")
    result = box.run(SCRIPT)
    assert result.status == 1 and "PGP" in result.stderr
    assert not box.path(KEYRING).exists() and not box.path(SOURCES).exists()


def test_stops_when_apt_fails(box):
    fresh(box)
    box.answer(f"{APT} {CONF} install -y {PACKAGES} jq", status=100)
    result = box.run(SCRIPT)
    assert result.status == 100
    assert "systemctl enable --quiet --now docker" not in result.calls


def test_stops_when_compose_still_does_not_work_after_the_install(box):
    fresh(box)
    box.answer(COMPOSE, status=1)
    result = box.run(SCRIPT)
    assert result.status == 1 and "docker compose" in result.stderr


def test_a_failure_status_of_255_becomes_1(box):
    fresh(box)
    box.answer("systemctl enable --quiet --now docker", status=255)
    assert box.run(SCRIPT).status == 1
