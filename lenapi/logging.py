"""Public standard-library loggers for lenApi applications."""

import logging as _logging


logger = _logging.getLogger("lenapi")
logger.setLevel(_logging.INFO)

if not any(getattr(handler, "_lenapi_default_handler", False) for handler in logger.handlers):
    _handler = _logging.StreamHandler()
    _handler.setFormatter(_logging.Formatter("%(levelname)s: %(message)s"))
    _handler._lenapi_default_handler = True
    logger.addHandler(_handler)

# LenApi owns this default handler; don't also send the same records to a
# configured root logger, which would print duplicates under Uvicorn.
logger.propagate = False


def get_logger(name: str | None = None) -> _logging.Logger:
    """Return lenApi's default logger or a named child logger."""
    if not name:
        return logger
    return _logging.getLogger(f"lenapi.{name}")
