"""Platform-specific locations used by cnsr on the local machine."""

import sys
from collections.abc import Mapping
from pathlib import Path


def config_dir(env: Mapping[str, str], platform: str, home: Path) -> Path:
    """Directory holding cnsr's own files (for example secrets.env)."""
    if platform == "win32":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "cnsr"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    return base / "cnsr"


def ssh_config_path(home: Path) -> Path:
    """The user's OpenSSH client config file."""
    return home / ".ssh" / "config"


def pub_dir(home: Path) -> Path:
    """Directory where one public key per server alias is kept."""
    return home / ".ssh" / "pub"


def key_dir(home: Path) -> Path:
    """Directory where the key pairs cnsr makes with ssh-keygen are kept, one per server alias."""
    return home / ".ssh" / "cnsr"


def private_dir(directory: Path) -> None:
    """Create ``directory`` (and its parents) readable by this user only, unless it exists."""
    if directory.exists():
        return
    directory.mkdir(parents=True, mode=0o700)
    if sys.platform != "win32":
        directory.chmod(0o700)
