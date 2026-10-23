"""Reading and editing the Host blocks cnsr manages in the user's OpenSSH client config.

The file also holds entries the user wrote by hand. cnsr owns only the ``Host`` line of an
alias and the managed keyword lines under it (HostName, User, Port, IdentityFile,
IdentitiesOnly, IdentityAgent). Every other line, inside or outside the block, is kept
exactly as it was, including its line ending, a BOM and bytes that are not valid UTF-8.
Lines cnsr writes always end in LF.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from cnsr.errors import CnsrError
from cnsr.paths import private_dir

BACKUP_NAME = "config_bak_cnsr"
# Held next to the config while cnsr reads, edits and writes it, so two runs never drop each
# other's block. It stays in place: deleting it would let a third run lock a different file.
LOCK_NAME = "config_lock_cnsr"

_BOM = b"\xef\xbb\xbf"
_LINE = re.compile(r"[^\n]*\n|[^\n]+")
_DIRECTIVE = re.compile(r"[ \t]*([A-Za-z][A-Za-z0-9]*)(?:[ \t]*=[ \t]*|[ \t]+)(.*)")
_ARG = re.compile(r"\"([^\"]*)\"|'([^']*)'|([^\s\"']+)")
_WILDCARDS = "*?!"
_READ_KEYWORDS = ("hostname", "user", "port", "identityfile", "identityagent")
_MANAGED = ("hostname", "user", "port", "identityfile", "identitiesonly", "identityagent")
# The config files this process has written once: their backup already holds the file as it
# was before this cnsr run, and later writes in the same run leave it alone.
_backed_up: set[str] = set()


@dataclass
class HostBlock:
    """The fields cnsr manages in one ``Host <alias>`` block."""

    alias: str
    hostname: str
    user: str
    port: int
    identity_file: str
    identity_agent: str | None = None


@dataclass
class _Document:
    raw: bytes | None  # None when the file does not exist yet
    bom: bytes
    lines: list[str]


def read_block(path: Path, alias: str) -> HostBlock | None:
    """Return the block for ``alias``, or None when there is no plain ``Host <alias>``.

    Host lines with several patterns or any wildcard never match. When the alias appears
    more than once the first block wins, as it does for ssh itself.
    """
    lines = _load(path).lines
    spans = _find_blocks(lines, alias)
    if not spans:
        return None
    values = _values(lines, *spans[0])
    port = values.get("port", "22")
    return HostBlock(
        alias=alias,
        hostname=values.get("hostname", ""),
        user=values.get("user", ""),
        port=int(port) if port.isdigit() else 22,
        identity_file=values.get("identityfile", ""),
        identity_agent=values.get("identityagent"),
    )


def write_block(path: Path, block: HostBlock) -> None:
    """Bring the managed lines of ``Host <block.alias>`` in line with ``block``, or append it.

    In an existing block the Host line and every line that is not managed stay as they are.
    A managed line with the wanted value is kept, one with another value is rewritten in
    place, unwanted and repeated ones are deleted, and missing ones are inserted after the
    last managed line (right after the Host line when there is none). A new block goes
    before the first block that also applies to the alias (``Host *``, a matching pattern or
    any ``Match``) with the comments right above it, since ssh takes the first value it finds;
    with no such block it is appended. A blank line separates it from its neighbours.

    On the first write of this process the file is copied to ``config_bak_cnsr`` next to it,
    so the backup holds it as it was before this cnsr run. Nothing is written when the result
    would be identical to the current file.
    """
    _validate(block)
    with _locked(path):
        document = _load(path)
        lines = document.lines
        regions = _find_blocks(lines, block.alias)
        new = [f"Host {block.alias}\n", *(setting.line for setting in _settings(block).values())]
        if regions:
            start, end = regions[0]
            lines[start:end] = _update_region(lines[start:end], block)
        elif (at := _ahead_of(lines, block.alias)) is not None:
            lines[at:at] = [*new, "\n"]
        else:
            if lines:
                if not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                if not _is_blank(lines[-1]):
                    lines.append("\n")
            lines.extend(new)
        _save(path, document)


def remove_block(path: Path, alias: str, hostnames: set[str] | None = None) -> bool:
    """Delete the managed lines of every plain ``Host <alias>`` block.

    With ``hostnames``, only the blocks whose HostName is one of them are touched; a block
    pointing elsewhere (or without a HostName) is someone else's and stays as it is.

    The Host line goes too, with one blank separator line, unless the rest of the block holds
    directives of the user's own; then the Host line stays with them. Returns False, without
    touching the file, when there was nothing to delete.
    """
    with _locked(path):
        return _remove_block(path, alias, hostnames)


def _remove_block(path: Path, alias: str, hostnames: set[str] | None) -> bool:
    document = _load(path)
    lines = document.lines
    drop: set[int] = set()
    for start, end in _find_blocks(lines, alias):
        if hostnames is not None and _values(lines, start, end).get("hostname", "") not in hostnames:
            continue
        owned = {i for i in range(start + 1, end) if _is_managed(lines[i])}
        rest = [lines[i] for i in range(start + 1, end) if i not in owned]
        if all(_directive(line) is None for line in rest):
            owned.add(start)
            owned |= _separator(lines, start, owned)
        drop |= owned
    if not drop:
        return False
    lines[:] = [line for i, line in enumerate(lines) if i not in drop]
    _save(path, document)
    return True


def _values(lines: list[str], start: int, end: int) -> dict[str, str]:
    """The first value of each readable keyword in the block ``[start, end)``, keyed in lower case."""
    values: dict[str, str] = {}
    for line in lines[start + 1 : end]:
        directive = _directive(line)
        if directive and directive[0] in _READ_KEYWORDS and directive[0] not in values:
            args = _split_args(directive[1])
            values[directive[0]] = args[0] if args else ""
    return values


def _directive(line: str) -> tuple[str, str] | None:
    """Split a config line into a lower-case keyword and its argument text."""
    match = _DIRECTIVE.fullmatch(line.rstrip("\r\n"))
    return (match.group(1).lower(), match.group(2)) if match else None


def _split_args(text: str) -> list[str]:
    """Split arguments like ssh does: quotes group words, an unquoted ``#`` starts a comment."""
    args: list[str] = []
    for match in _ARG.finditer(text):
        quoted = match.group(1) if match.group(1) is not None else match.group(2)
        if quoted is not None:
            args.append(quoted)
        elif match.group(3).startswith("#"):
            break
        else:
            args.append(match.group(3))
    return args


def _is_blank(line: str) -> bool:
    return not line.strip()


def _plain_alias(line: str) -> str | None:
    """The only pattern of a ``Host`` line, or None for other lines, lists and wildcards."""
    directive = _directive(line)
    if directive is None or directive[0] != "host":
        return None
    patterns = _split_args(directive[1])
    if len(patterns) != 1 or any(char in patterns[0] for char in _WILDCARDS):
        return None
    return patterns[0]


def _is_managed(line: str) -> bool:
    directive = _directive(line)
    return directive is not None and directive[0] in _MANAGED


def _ahead_of(lines: list[str], alias: str) -> int | None:
    """Where a new block for ``alias`` must go: before the first ``Match`` or ``Host`` line that
    applies to it, and the comment lines right above that; None when no block applies."""
    for i, line in enumerate(lines):
        directive = _directive(line)
        if directive is None or directive[0] not in ("host", "match"):
            continue
        if directive[0] == "match" or _applies(_split_args(directive[1]), alias):
            while i > 0 and lines[i - 1].lstrip().startswith("#"):
                i -= 1
            return i
    return None


def _applies(patterns: list[str], alias: str) -> bool:
    """Whether a ``Host`` line with ``patterns`` applies to ``alias``, as ssh decides it."""
    if any(p.startswith("!") and fnmatch.fnmatchcase(alias, p[1:]) for p in patterns):
        return False
    return any(not p.startswith("!") and fnmatch.fnmatchcase(alias, p) for p in patterns)


def _find_blocks(lines: list[str], alias: str) -> list[tuple[int, int]]:
    """Regions ``[start, end)`` of the ``Host <alias>`` blocks.

    A block runs from its Host line to the next ``Host`` or ``Match`` line.
    """
    starts = [i for i, line in enumerate(lines) if (d := _directive(line)) and d[0] in ("host", "match")]
    ends = starts[1:] + [len(lines)]
    return [(start, end) for start, end in zip(starts, ends) if _plain_alias(lines[start]) == alias]


def _separator(lines: list[str], start: int, owned: set[int]) -> set[int]:
    """The blank line to delete along with a block that is removed completely, if any.

    Only a block whose lines are contiguous leaves a separator behind: the blank line after
    it, or at the end of the file the one before it.
    """
    last = max(owned)
    if owned != set(range(start, last + 1)):
        return set()
    if last + 1 < len(lines) and _is_blank(lines[last + 1]):
        return {last + 1}
    if last + 1 == len(lines) and start > 0 and _is_blank(lines[start - 1]):
        return {start - 1}
    return set()


class _Setting(NamedTuple):
    name: str  # keyword as written, for example "HostName"
    value: str  # the value without quotes
    text: str  # the value as written in the file

    @property
    def line(self) -> str:
        return f"    {self.name} {self.text}\n"

    def matches(self, args: list[str]) -> bool:
        if self.name == "IdentitiesOnly":
            return [arg.lower() for arg in args] == [self.value]
        return args == [self.value]


def _settings(block: HostBlock) -> dict[str, _Setting]:
    """The managed lines wanted for ``block``, keyed by lower-case keyword, in writing order."""
    identity_file = block.identity_file
    if any(char.isspace() or char == "'" for char in identity_file):
        identity_file_text = f'"{identity_file}"'
    else:
        identity_file_text = identity_file
    settings = {"hostname": _Setting("HostName", block.hostname, block.hostname)}
    if block.user:
        settings["user"] = _Setting("User", block.user, block.user)
    settings["port"] = _Setting("Port", str(block.port), str(block.port))
    settings["identityfile"] = _Setting("IdentityFile", identity_file, identity_file_text)
    settings["identitiesonly"] = _Setting("IdentitiesOnly", "yes", "yes")
    if block.identity_agent is not None:
        settings["identityagent"] = _Setting("IdentityAgent", block.identity_agent, f'"{block.identity_agent}"')
    return settings


def _update_region(region: list[str], block: HostBlock) -> list[str]:
    """Return ``region`` (a Host line and what follows) with its managed lines matching ``block``."""
    wanted = _settings(block)
    result = [region[0]]
    seen: set[str] = set()
    anchor = 0  # index in result of the Host line or the last managed line kept
    for line in region[1:]:
        directive = _directive(line)
        if directive is None or directive[0] not in _MANAGED:
            result.append(line)
            continue
        keyword = directive[0]
        if keyword in seen or keyword not in wanted:
            continue
        seen.add(keyword)
        result.append(line if wanted[keyword].matches(_split_args(directive[1])) else wanted[keyword].line)
        anchor = len(result) - 1
    missing = [setting.line for keyword, setting in wanted.items() if keyword not in seen]
    if missing:
        if not result[anchor].endswith("\n"):
            result[anchor] += "\n"
        result[anchor + 1 : anchor + 1] = missing
    return result


def _validate(block: HostBlock) -> None:
    def bad(field: str) -> CnsrError:
        return CnsrError(f"SSH 别名 {block.alias!r} 的{field}含有不能写入 SSH 配置的内容。")

    def bad_word(value: str, extra: str = "") -> bool:
        return any(char.isspace() or char in "\"'#" + extra for char in value)

    def bad_path(value: str) -> bool:
        # A double quote or newline cannot be written inside the quotes; a trailing
        # backslash would escape the closing quote.
        return any(char in '"\r\n' for char in value) or value.endswith("\\")

    if not block.alias or bad_word(block.alias, _WILDCARDS + "="):
        raise bad("别名")
    if not block.hostname or bad_word(block.hostname):
        raise bad("主机名")
    if bad_word(block.user):
        raise bad("用户名")
    if not 1 <= block.port <= 65535:
        raise bad("端口")
    if not block.identity_file or bad_path(block.identity_file):
        raise bad("公钥路径")
    if block.identity_agent is not None and bad_path(block.identity_agent):
        raise bad("agent 路径")


def _load(path: Path) -> _Document:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _Document(None, b"", [])
    except OSError as exc:
        raise CnsrError(f"无法读取 SSH 配置文件 {path}。") from exc
    bom = _BOM if raw.startswith(_BOM) else b""
    text = raw[len(bom) :].decode("utf-8", errors="surrogateescape")
    return _Document(raw, bom, _LINE.findall(text))


def _save(path: Path, document: _Document) -> None:
    data = document.bom + "".join(document.lines).encode("utf-8", errors="surrogateescape")
    if data == document.raw:
        return
    try:
        private_dir(path.parent)
        first = str(path.absolute()) not in _backed_up
        if first and document.raw is not None:
            _write_private(path.parent / BACKUP_NAME, document.raw)
        _backed_up.add(str(path.absolute()))
        # A symlinked config (dotfile managers) must stay a symlink: write to its target.
        _write_private(Path(os.path.realpath(path)), data)
    except OSError as exc:
        raise CnsrError(f"无法写入 SSH 配置文件 {path}。") from exc


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``LOCK_NAME`` next to ``path``; another cnsr run waits until it is released."""
    private_dir(path.parent)
    with open(path.parent / LOCK_NAME, "a+b") as handle:
        try:
            import fcntl
        except ImportError:  # Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield  # closing the file releases the lock


def _write_private(target: Path, data: bytes) -> None:
    """Replace ``target`` atomically with ``data``, readable by the current user only on POSIX."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())  # on disk before the rename, or a crash can leave an empty file
        if sys.platform != "win32":
            tmp.chmod(0o600)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
