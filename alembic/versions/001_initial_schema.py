"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-02-23
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op as _op
    bind = _op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "teams" in existing_tables:
        return

    op.create_table(
        "teams",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("plan", sa.String(50), nullable=True),
        sa.Column("monthly_budget", sa.Float, default=0.0),
    )

    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(100), primary_key=True),
        sa.Column("team_id", sa.String(50), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("agent_name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), default="running"),
        sa.Column("priority", sa.String(20), nullable=True),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("input_tokens", sa.Integer, default=0),
        sa.Column("output_tokens", sa.Integer, default=0),
        sa.Column("cached_tokens", sa.Integer, default=0),
        sa.Column("total_cost", sa.Float, default=0.0),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float, default=0.0),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("tags", sa.String(500), nullable=True),
    )

    op.create_table(
        "token_usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(100), sa.ForeignKey("agent_sessions.session_id"), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True)),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer, default=0),
        sa.Column("output_tokens", sa.Integer, default=0),
        sa.Column("cached_tokens", sa.Integer, default=0),
        sa.Column("cost", sa.Float, default=0.0),
    )

    op.create_table(
        "usage_requests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True)),
        sa.Column("caller_service", sa.String(100)),
        sa.Column("method", sa.String(10)),
        sa.Column("route_template", sa.String(500)),
        sa.Column("status_code", sa.Integer),
        sa.Column("duration_ms", sa.Float),
    )

    op.create_table(
        "contract_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("git_sha", sa.String(64), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "contract_changes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("base_ref", sa.String(64), nullable=False),
        sa.Column("head_ref", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("is_breaking", sa.Boolean, default=False),
        sa.Column("severity", sa.String(20)),
        sa.Column("summary_json", sa.Text),
        sa.Column("changed_routes_json", sa.Text),
        sa.Column("changed_fields_json", sa.Text),
    )

    op.create_table(
        "impact_sets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("change_id", sa.Integer, sa.ForeignKey("contract_changes.id"), nullable=False),
        sa.Column("route_template", sa.String(500), nullable=False),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("caller_service", sa.String(100), nullable=False),
        sa.Column("calls_last_7d", sa.Integer, default=0),
        sa.Column("confidence", sa.String(20), default="high"),
        sa.Column("notes", sa.Text, nullable=True),
    )

    op.create_table(
        "remediation_jobs",
        sa.Column("job_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("change_id", sa.Integer, sa.ForeignKey("contract_changes.id"), nullable=False),
        sa.Column("target_repo", sa.String(500), nullable=False),
        sa.Column("status", sa.String(30), default="queued"),
        sa.Column("devin_run_id", sa.String(200), nullable=True),
        sa.Column("pr_url", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("bundle_hash", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text, nullable=True),
        sa.Column("is_dry_run", sa.Boolean, default=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("remediation_jobs.job_id"), nullable=False),
        sa.Column("old_status", sa.String(30), nullable=True),
        sa.Column("new_status", sa.String(30), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True)),
        sa.Column("detail", sa.Text, nullable=True),
    )

    op.create_table(
        "service_dependencies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("service_name", sa.String(200), nullable=False),
        sa.Column("depends_on", sa.String(200), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("service_dependencies")
    op.drop_table("audit_log")
    op.drop_table("remediation_jobs")
    op.drop_table("impact_sets")
    op.drop_table("contract_changes")
    op.drop_table("contract_snapshots")
    op.drop_table("usage_requests")
    op.drop_table("token_usage")
    op.drop_table("agent_sessions")
    op.drop_table("teams")
