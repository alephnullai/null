"""Null Memory — Persistent agent memory for AI."""

from importlib import metadata as _metadata

try:
    # Authoritative: the version of the *installed* distribution. In an
    # editable install this still reflects what `pip` recorded, so two
    # installs of different versions answer differently — which is exactly
    # what `null doctor`'s install-integrity scan relies on to tell them
    # apart.
    __version__ = _metadata.version("null-memory")
except _metadata.PackageNotFoundError:  # pragma: no cover - source tree w/o install
    try:
        from null_memory.__version__ import __version__  # type: ignore
    except Exception:
        __version__ = "0+unknown"

__all__ = ["__version__"]
