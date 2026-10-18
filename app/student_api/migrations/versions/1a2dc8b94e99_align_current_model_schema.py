"""align current model schema

Revision ID: 1a2dc8b94e99
Revises: 7783d219fc60
Create Date: 2026-10-18 13:06:37.222905
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '1a2dc8b94e99'
down_revision: Union[str, None] = '7783d219fc60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = 'lenapi_background_jobs'
    if table_name not in inspector.get_table_names():
        op.create_table(
            table_name,
            sa.Column('job_id', sa.String(length=36), nullable=False),
            sa.Column('task_name', sa.String(length=200), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('available_at', sa.Float(), nullable=False),
            sa.Column('locked_until', sa.Float(), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.Float(), nullable=False),
            sa.Column('completed_at', sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint('job_id'),
        )
        inspector = sa.inspect(bind)
    existing_indexes = {index['name'] for index in inspector.get_indexes(table_name)}
    for index_name, column in (
        ('ix_lenapi_background_jobs_available_at', 'available_at'),
        ('ix_lenapi_background_jobs_status', 'status'),
        ('ix_lenapi_background_jobs_task_name', 'task_name'),
    ):
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, [column], unique=False)

    # The source schema update was generated against MySQL. Existing SQLite
    # databases retain stricter NOT NULL settings rather than using MySQL-only
    # ALTER syntax; fresh schemas already use model metadata.
    if bind.dialect.name != 'sqlite':
        nullable_columns = {
            'classroom': ('name',),
            'photo': ('image',),
            'student': ('person_name', 'course'),
            'user': ('username', 'first_name', 'last_name'),
        }
        for table, columns in nullable_columns.items():
            current_columns = {column['name']: column for column in sa.inspect(bind).get_columns(table)}
            for column in columns:
                current = current_columns[column]
                if not current['nullable']:
                    op.alter_column(table, column, existing_type=current['type'], nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = 'lenapi_background_jobs'
    if table_name not in inspector.get_table_names():
        return
    for index in inspector.get_indexes(table_name):
        if index['name'].startswith('ix_lenapi_background_jobs_'):
            op.drop_index(index['name'], table_name=table_name)
    op.drop_table(table_name)
