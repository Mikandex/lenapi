"""Public standard-library loggers for lenApi applications."""

import logging as _logging


logger = _logging.getLogger("lenapi")


def get_logger(name: str | None = None) -> _logging.Logger:
    """Return lenApi's default logger or a named child logger."""
    if not name:
        return logger
    return _logging.getLogger(f"lenapi.{name}")
