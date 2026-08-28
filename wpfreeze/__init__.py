"""wpfreeze — WordPress site acquisition and inventory tool."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: pyproject.toml's `version`, read from the
    # installed distribution's metadata. Hardcoding it here as well meant
    # two copies that could drift silently, with nothing to catch it.
    __version__ = version("wpfreeze")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
