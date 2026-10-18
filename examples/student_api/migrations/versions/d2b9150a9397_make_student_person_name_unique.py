"""make student person_name unique

Revision ID: d2b9150a9397
Revises: 1a2dc8b94e99
Create Date: 2026-10-18 13:14:01.304788
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2b9150a9397'
down_revision: Union[str, None] = '1a2dc8b94e99'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('student') as batch_op:
        batch_op.create_unique_constraint('uq_student_person_name', ['person_name'])


def downgrade() -> None:
    with op.batch_alter_table('student') as batch_op:
        batch_op.drop_constraint('uq_student_person_name', type_='unique')
