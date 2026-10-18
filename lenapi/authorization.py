"""Reusable role, permission, and object ownership dependencies."""

import inspect
from collections.abc import Mapping
from typing import Any, Callable

from .dependencies import Depends
from .request import Request
from .response import HTTPException


def _claim(identity: Any, *names: str) -> Any:
    if isinstance(identity, Mapping):
        for name in names:
            if name in identity:
                return identity[name]
    elif isinstance(identity, (str, int)) and {"sub", "user_id", "id"}.intersection(names):
        return identity
    return None


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value}
    return {str(value)}


def require_role(*roles: str, auth: Callable) -> Callable:
    """Build a dependency that requires at least one named role."""
    required = {role for role in roles if role}
    if not required:
        raise ValueError("require_role needs at least one role")

    async def check_role(identity=Depends(auth)):
        actual = _as_set(_claim(identity, "roles", "role"))
        if not required.intersection(actual):
            raise HTTPException(403, "Insufficient role")
        return identity

    return check_role


def require_permission(*permissions: str, auth: Callable, match_all: bool = False) -> Callable:
    """Build a dependency that checks permissions or OAuth-style scopes."""
    required = {permission for permission in permissions if permission}
    if not required:
        raise ValueError("require_permission needs at least one permission")

    async def check_permission(identity=Depends(auth)):
        actual = _as_set(_claim(identity, "permissions", "scopes", "scope"))
        allowed = required.issubset(actual) if match_all else bool(required.intersection(actual))
        if not allowed:
            raise HTTPException(403, "Insufficient permission")
        return identity

    return check_permission


def require_owner(
    load_object: Callable[[str], Any],
    *,
    auth: Callable,
    path_param: str = "id",
    owner_field: str = "user_id",
) -> Callable:
    """Build a dependency that loads a route object and permits only its owner.

    ``load_object`` receives the string path parameter and returns the model row
    (or ``None``). The authenticated identity must contain ``sub``, ``user_id``,
    or ``id``. The loaded object is returned to the endpoint.
    """
    async def check_owner(request: Request, identity=Depends(auth)):
        object_id = request.path_params.get(path_param)
        if object_id is None:
            raise HTTPException(400, f"Missing object path parameter '{path_param}'")
        resource = load_object(str(object_id))
        if inspect.isawaitable(resource):
            resource = await resource
        if resource is None:
            raise HTTPException(404, "Not found")

        if isinstance(resource, Mapping):
            owner_id = resource.get(owner_field)
        else:
            owner_id = getattr(resource, owner_field, None)
        subject = _claim(identity, "sub", "user_id", "id")
        if owner_id is None or subject is None or str(owner_id) != str(subject):
            # Hide whether another user's object exists.
            raise HTTPException(404, "Not found")
        return resource

    return check_owner
