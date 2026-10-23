"""Request parameter markers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Body:
    """Read a JSON request body without applying a schema or validation."""


@dataclass(frozen=True)
class File:
    """Read an uploaded multipart file; ``File(...)`` marks it as required."""

    default: Any = ...
