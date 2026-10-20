import subprocess
import sys
import textwrap


def test_user_subclass_extends_existing_table_and_migration_detects_columns(tmp_path):
    database_path = tmp_path / "user-extension.sqlite3"
    script = textwrap.dedent(
        """
        import sys

        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from sqlalchemy import false

        from lenapi import model
        from lenapi.database import Database
        from lenapi.models import Model, User as BaseUser

        database = Database(f"sqlite:///{sys.argv[1]}")
        Model.metadata.create_all(database.engine)

        class User(BaseUser):
            bio = model.TextField(default="", nullable=False, server_default="")
            is_verified = model.BooleanField(
                default=False, nullable=False, server_default=false()
            )

        assert User.__table__ is BaseUser.__table__
        assert User.__table__.name == "user"
        assert User.__table__.c.is_verified.default.arg is False

        with database.engine.connect() as connection:
            differences = compare_metadata(
                MigrationContext.configure(connection), Model.metadata
            )

        added_user_columns = {
            difference[3].name
            for difference in differences
            if difference[0] == "add_column" and difference[2] == "user"
        }
        assert {"bio", "is_verified"}.issubset(added_user_columns)
        database.close()
        """
    )
    subprocess.run(
        [sys.executable, "-c", script, str(database_path)],
        check=True,
        capture_output=True,
        text=True,
    )
