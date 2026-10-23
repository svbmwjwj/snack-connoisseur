"""Reading and writing the local secrets.env file."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cnsr.errors import CnsrError

KEYS = (
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ZONE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "CNSR_SSH_AGENT",
)
# The credentials the run log must never show; CLOUDFLARE_ZONE and CNSR_SSH_AGENT are not secret.
SECRET_KEYS = ("CLOUDFLARE_API_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def load(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines, tolerating a BOM, CRLF, comments and paired quotes."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise CnsrError(f"{path} 不是 UTF-8 文本，无法读取，请用文本编辑器另存为 UTF-8 后重跑。") from exc
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = _unquote(value.strip())
    from cnsr import runlog  # imported here because runlog itself imports this module

    for name in SECRET_KEYS:
        runlog.secret(values.get(name))
    return values


def save(path: Path, values: dict[str, str], *, platform: str = sys.platform, run=subprocess.run) -> None:
    """Write values atomically as LF text and restrict the file to the current user.

    Known keys come from `values` in the order of KEYS; a known key missing from `values` is
    dropped from the file, so callers pass `{**load(path), ...}`. Unknown keys already in the
    file are kept, unless `values` carries them; extra unknown keys in `values` are
    appended after them.
    """
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise CnsrError(f"凭据 {key} 的值不能包含换行")
    if platform == "win32":
        _windows_user()  # fail before anything is written

    unknown = {k: v for k, v in load(path).items() if k not in KEYS}
    unknown.update({k: v for k, v in values.items() if k not in KEYS})
    lines = [f"{k}={_format_value(values[k])}" for k in KEYS if k in values]
    lines += [f"{k}={_format_value(v)}" for k, v in unknown.items()]
    data = "".join(f"{line}\n" for line in lines).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if platform != "win32":
            tmp.chmod(0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    if platform == "win32":
        restrict_to_user(path, platform=platform, run=run)


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"


def _unquote(value: str) -> str:
    return value[1:-1] if _is_quoted(value) else value


def _format_value(value: str) -> str:
    """Return the text to write so that `load` reads back exactly `value`.

    `load` strips surrounding whitespace and removes one pair of matching quotes, so
    values that would lose either get wrapped in one extra pair of double quotes.
    """
    if value != value.strip() or _is_quoted(value):
        return f'"{value}"'
    return value


def _windows_user() -> str:
    username = os.environ.get("USERNAME", "")
    if not username:
        raise CnsrError("无法确定当前 Windows 用户名，不能限制文件的访问权限")
    return username


def restrict_to_user(path: Path, *, platform: str = sys.platform, run=subprocess.run) -> None:
    """Limit a file to the current user: chmod 600 on Unix, icacls on Windows."""
    if platform != "win32":
        path.chmod(0o600)
        return
    username = _windows_user()
    try:
        run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CnsrError(f"无法限制文件的访问权限: {path}") from exc
