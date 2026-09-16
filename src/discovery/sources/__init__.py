"""Discovery source adapters.

Importing this package registers every implemented adapter with
:mod:`src.discovery.registry`. A new source is added by dropping a module here
and importing it below — no other pipeline stage changes.
"""

from __future__ import annotations

from src.discovery.sources import aitoolnet as aitoolnet  # noqa: F401
from src.discovery.sources import creati as creati  # noqa: F401
from src.discovery.sources import taaft as taaft  # noqa: F401

__all__ = ["aitoolnet", "creati", "taaft"]
