"""Alembic render helpers for portable LenAPI migrations."""

from __future__ import annotations

from sqlalchemy.sql.elements import False_, True_


def render_migration_item(type_: str, item, autogen_context):
    """Keep Boolean server defaults as Boolean expressions in revisions.

    Alembic renders ``false()`` using the dialect used to generate a revision.
    On SQLite that becomes ``sa.text('0')``; such a revision then fails when
    applied to PostgreSQL. Preserve the expression so the target dialect
    compiles it to its native Boolean literal at migration time.
    """
    if type_ != "server_default":
        return False

    expression = getattr(item, "arg", item)
    sqlalchemy_prefix = autogen_context.opts.get("sqlalchemy_module_prefix", "sa.")
    if isinstance(expression, False_):
        return f"{sqlalchemy_prefix}false()"
    if isinstance(expression, True_):
        return f"{sqlalchemy_prefix}true()"
    return False
