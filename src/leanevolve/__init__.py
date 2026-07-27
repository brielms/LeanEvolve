"""Kernel-scored Lean proof discovery infrastructure."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lean-evolve")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
