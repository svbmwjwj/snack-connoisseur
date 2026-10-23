"""cnsr: deploy Xray REALITY proxy nodes."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "snack-connoisseur"


def _installed_version(lookup=version) -> str:
    """The version pyproject.toml gives the installed package; a placeholder when it is not installed."""
    try:
        return lookup(DISTRIBUTION)
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _installed_version()
