"""The Whodunit Curve — watch Jev's belief about the killer shift across a story.

Public surface:
    from detective_jev import query      # the Jev client
    from detective_jev import config     # model id, limits, pricing, paths
"""

from __future__ import annotations

from .jev_client import JevError, query

__all__ = ["query", "JevError"]
