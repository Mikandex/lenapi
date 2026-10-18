"""Alembic environment for lenApi project models."""

from importlib import import_module

from alembic import context
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from lenapi import Model

config = context.config


def load_app():
    app_path = config.attributes.get("app_import_string", "main:app")
    module_name, _, attribute_path = app_path.partition(":")
    app = import_module(module_name)
    for name in attribute_path.split(".") if attribute_path else []:
        app = getattr(app, name)
    return app


def run_migrations_offline() -> None:
    app = load_app()
    url = app.database.engine.url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=Model.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    app = load_app()
    engine: Engine = app.database.engine

    def configure_and_run(connection) -> None:
        context.configure(
            connection=connection,
            target_metadata=Model.metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()

    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        configure_and_run(supplied_connection)
        return

    with engine.connect() as connection:
        existing_tables = set(inspect(connection).get_table_names()) - {"alembic_version"}
        # Inspector queries autobegin a SQLAlchemy 2.x transaction. End that
        # read transaction so Alembic owns and commits its migration transaction.
        if connection.in_transaction():
            connection.commit()
        initial = config.attributes.get("lenapi_initial_revision", False)
        if initial and existing_tables and connection.dialect.name == "sqlite":
            # Generate the first revision against an empty schema, even when
            # an older development database was created before migrations.
            with create_engine("sqlite://").connect() as empty_connection:
                configure_and_run(empty_connection)
        else:
            configure_and_run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
