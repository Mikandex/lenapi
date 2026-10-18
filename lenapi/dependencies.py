"""Dependency declaration helpers."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Depends:
    """Mark a parameter for resolution from another callable."""

    dependency: Callable[..., Any]
