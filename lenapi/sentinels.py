"""Sentinel values used to distinguish omitted arguments from explicit ``None``."""


def _get_unset():
    return UNSET


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (_get_unset, ())


UNSET = _Unset()
