"""products: barcode con índice no único

`INSERT ... ON CONFLICT` solo arbitra un índice único (uq_products_source_external_id).
Con `uq_products_barcode` además, dos escaneos simultáneos del mismo código podían
tronar con UniqueViolationError en ese índice. La unicidad del barcode ya la
garantizan el CHECK `barcode_only_off` (barcode = external_id y source = 'off') y
unique(source, external_id). Ver ADR 0006 (D5).

Revision ID: 085adeb10b5e
Revises: beca2b5cb6bf
Create Date: 2026-10-15 17:45:44.005932+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "085adeb10b5e"
down_revision: str | Sequence[str] | None = "beca2b5cb6bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("uq_products_barcode"), "products", type_="unique")
    op.create_index(op.f("ix_products_barcode"), "products", ["barcode"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_products_barcode"), table_name="products")
    op.create_unique_constraint(op.f("uq_products_barcode"), "products", ["barcode"])
