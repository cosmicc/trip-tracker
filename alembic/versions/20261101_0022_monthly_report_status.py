"""Track monthly PDF downloads and reimbursement submissions."""

import sqlalchemy as sa

from alembic import op

revision = "20261101_0022"
down_revision = "20260711_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add monthly report workflow state without changing existing trip data."""
    op.create_table(
        "monthly_report_statuses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("download_fingerprint", sa.String(64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("year", "month", name="uq_report_status_month"),
    )


def downgrade() -> None:
    """Remove report workflow state; retain trips and expenses."""
    op.drop_table("monthly_report_statuses")
