"""Regression tests for portable generated database defaults."""

from types import SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, MetaData, Table, false, true
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable, DefaultClause

from lenapi.migration_render import render_migration_item
from lenapi.models import BooleanField


@pytest.mark.parametrize(
    ("expression", "expected_sql", "expected_render"),
    [
        (false(), "false", "sa.false()"),
        (true(), "true", "sa.true()"),
    ],
)
def test_boolean_field_server_defaults_are_portable(
    expression, expected_sql, expected_render
):
    field = BooleanField(default=False, nullable=False, server_default=expression)
    default = field.column.server_default

    assert str(default.arg) == expected_sql
    for dialect in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
        table = Table(
            f"boolean_default_{dialect.name}",
            MetaData(),
            Column("enabled", Boolean, server_default=default.arg, nullable=False),
        )
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"default {expected_sql}" in ddl.lower()

    rendered = render_migration_item(
        "server_default",
        DefaultClause(expression),
        SimpleNamespace(opts={"sqlalchemy_module_prefix": "sa."}),
    )
    assert rendered == expected_render


def test_non_boolean_server_default_uses_alembic_default_renderer():
    assert render_migration_item(
        "server_default", "CURRENT_TIMESTAMP", SimpleNamespace(opts={})
    ) is False
