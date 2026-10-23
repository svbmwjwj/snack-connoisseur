import subprocess

import pytest

from cnsr.errors import CnsrError
from cnsr.ssh import keygen


class Run:
    def __init__(self, returncode=0, make=None):
        self.returncode, self.make, self.calls = returncode, make, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.make:
            self.make()
        return subprocess.CompletedProcess(args, self.returncode)


def test_generate_runs_ssh_keygen_in_the_terminal(tmp_path):
    path = tmp_path / "jp-1"
    run = Run(make=lambda: path.write_text("private"))
    keygen.generate(path, "jp-1", run=run)
    ((args, kwargs),) = run.calls
    assert args == ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", "jp-1"]
    assert "capture_output" not in kwargs and "stdin" not in kwargs  # it asks for the passphrase itself


@pytest.mark.parametrize("run", [Run(returncode=1), Run()])  # failed, or made nothing
def test_a_failed_generate_is_an_error(tmp_path, run):
    with pytest.raises(CnsrError, match="ssh-keygen"):
        keygen.generate(tmp_path / "jp-1", "jp-1", run=run)


def test_has_passphrase_tries_to_open_the_key_with_none(tmp_path):
    run = Run(returncode=1)
    assert keygen.has_passphrase(tmp_path / "jp-1", run=run) is True
    ((args, kwargs),) = run.calls
    assert args == ["ssh-keygen", "-y", "-P", "", "-f", str(tmp_path / "jp-1")]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["capture_output"] is True
    assert keygen.has_passphrase(tmp_path / "jp-1", run=Run()) is False


def test_add_to_agent_keeps_the_passphrase_in_the_macos_keychain(tmp_path):
    run = Run()
    keygen.add_to_agent(tmp_path / "jp-1", platform="darwin", run=run)
    keygen.add_to_agent(tmp_path / "jp-1", platform="linux", run=run)
    # Apple's own ssh-add: one from Homebrew earlier in PATH does not know the keychain option
    assert [args for args, _ in run.calls] == [
        ["/usr/bin/ssh-add", "--apple-use-keychain", str(tmp_path / "jp-1")],
        ["ssh-add", str(tmp_path / "jp-1")],
    ]


def test_a_failed_add_to_agent_is_an_error(tmp_path):
    with pytest.raises(CnsrError, match="ssh-add"):
        keygen.add_to_agent(tmp_path / "jp-1", platform="linux", run=Run(returncode=2))
