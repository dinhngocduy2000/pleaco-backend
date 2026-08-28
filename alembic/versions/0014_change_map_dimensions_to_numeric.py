"""Change map dimensions to numeric values.

Revision ID: 0014_map_dimensions_numeric
Revises: 0013_map_list_indexes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014_map_dimensions_numeric"
down_revision: Union[str, None] = "0013_map_list_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    numeric_expression = (
        "CASE WHEN btrim({column}) ~ "
        "'^[+-]?((\\d+(\\.\\d*)?)|(\\.\\d+))([eE][+-]?\\d+)?$' "
        "THEN btrim({column})::numeric ELSE 0 END"
    )
    op.alter_column(
        "maps",
        "dimension_x",
        existing_type=sa.String(),
        type_=sa.Numeric(),
        postgresql_using=numeric_expression.format(column="dimension_x"),
    )
    op.alter_column(
        "maps",
        "dimension_y",
        existing_type=sa.String(),
        type_=sa.Numeric(),
        postgresql_using=numeric_expression.format(column="dimension_y"),
    )


def downgrade() -> None:
    op.alter_column(
        "maps",
        "dimension_y",
        existing_type=sa.Numeric(),
        type_=sa.String(),
        postgresql_using="dimension_y::text",
    )
    op.alter_column(
        "maps",
        "dimension_x",
        existing_type=sa.Numeric(),
        type_=sa.String(),
        postgresql_using="dimension_x::text",
    )
