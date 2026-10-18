"""Request parameter markers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Body:
    """Read a JSON request body without applying a schema or validation."""
