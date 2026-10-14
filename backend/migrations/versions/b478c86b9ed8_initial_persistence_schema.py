"""Initial persistence schema"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b478c86b9ed8'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('debates',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('client_request_id', sa.String(), nullable=False),
    sa.Column('topic_id', sa.String(), nullable=False),
    sa.Column('topic_snapshot', sa.JSON(), nullable=False),
    sa.Column('execution_token_hash', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('hidden', sa.Boolean(), nullable=False),
    sa.Column('hidden_reason', sa.Text(), nullable=True),
    sa.Column('hidden_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('current_round', sa.Integer(), nullable=False),
    sa.Column('active_speaker', sa.String(), nullable=True),
    sa.Column('active_turn', sa.Integer(), nullable=True),
    sa.Column('activity', sa.String(), nullable=True),
    sa.Column('accepted_attempts', sa.Integer(), nullable=False),
    sa.Column('total_calls', sa.Integer(), nullable=False),
    sa.Column('active_run_id', sa.String(), nullable=True),
    sa.Column('failed_stage', sa.String(), nullable=True),
    sa.Column('failed_turn', sa.Integer(), nullable=True),
    sa.Column('error_code', sa.String(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("activity IS NULL OR activity IN ('generating','retrying','evaluating')"),
    sa.CheckConstraint("status IN ('created','running','judging','completed','failed')"),
    sa.CheckConstraint('accepted_attempts BETWEEN 0 AND 3'),
    sa.CheckConstraint('total_calls BETWEEN 0 AND 30'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_request_id')
    )
    with op.batch_alter_table('debates', schema=None) as batch_op:
        batch_op.create_index('ix_debates_history', ['hidden', 'created_at'], unique=False)

    op.create_table('creation_quota_records',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('debate_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ip_bucket_hash', sa.String(), nullable=True),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['debate_id'], ['debates.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('debate_id')
    )
    with op.batch_alter_table('creation_quota_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_creation_quota_records_created_at'), ['created_at'], unique=False)
        batch_op.create_index('ix_creations_bucket_time', ['ip_bucket_hash', 'created_at'], unique=False)

    op.create_table('debate_messages',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('debate_id', sa.String(), nullable=False),
    sa.Column('agent', sa.String(), nullable=False),
    sa.Column('side', sa.String(), nullable=False),
    sa.Column('round_number', sa.Integer(), nullable=False),
    sa.Column('turn_order', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("agent IN ('atlas','nova')"),
    sa.CheckConstraint("side IN ('pro','con')"),
    sa.CheckConstraint('round_number BETWEEN 1 AND 3'),
    sa.CheckConstraint('turn_order BETWEEN 1 AND 6'),
    sa.ForeignKeyConstraint(['debate_id'], ['debates.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('debate_id', 'turn_order')
    )
    op.create_table('execution_attempts',
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('debate_id', sa.String(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('starting_checkpoint', sa.Integer(), nullable=False),
    sa.Column('failed_stage', sa.String(), nullable=True),
    sa.Column('failed_turn', sa.Integer(), nullable=True),
    sa.Column('model_id', sa.String(), nullable=False),
    sa.Column('prompt_version', sa.String(), nullable=False),
    sa.Column('call_count', sa.Integer(), nullable=False),
    sa.Column('ip_bucket_hash', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('active','completed','failed','interrupted')"),
    sa.ForeignKeyConstraint(['debate_id'], ['debates.id'], ),
    sa.PrimaryKeyConstraint('run_id'),
    sa.UniqueConstraint('debate_id', 'attempt_number')
    )
    with op.batch_alter_table('execution_attempts', schema=None) as batch_op:
        batch_op.create_index('ix_attempts_debate_status', ['debate_id', 'status'], unique=False)

    op.create_table('judgments',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('debate_id', sa.String(), nullable=False),
    sa.Column('pro_logical_reasoning', sa.Integer(), nullable=False),
    sa.Column('pro_relevance', sa.Integer(), nullable=False),
    sa.Column('pro_rebuttal_quality', sa.Integer(), nullable=False),
    sa.Column('pro_clarity', sa.Integer(), nullable=False),
    sa.Column('con_logical_reasoning', sa.Integer(), nullable=False),
    sa.Column('con_relevance', sa.Integer(), nullable=False),
    sa.Column('con_rebuttal_quality', sa.Integer(), nullable=False),
    sa.Column('con_clarity', sa.Integer(), nullable=False),
    sa.Column('pro_total', sa.Integer(), nullable=False),
    sa.Column('con_total', sa.Integer(), nullable=False),
    sa.Column('winner', sa.String(), nullable=False),
    sa.Column('pro_strengths', sa.Text(), nullable=False),
    sa.Column('pro_weaknesses', sa.Text(), nullable=False),
    sa.Column('con_strengths', sa.Text(), nullable=False),
    sa.Column('con_weaknesses', sa.Text(), nullable=False),
    sa.Column('overall_explanation', sa.Text(), nullable=False),
    sa.Column('referenced_message_ids', sa.JSON(), nullable=False),
    sa.Column('model_id', sa.String(), nullable=False),
    sa.Column('prompt_version', sa.String(), nullable=False),
    sa.Column('rubric_version', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("winner IN ('pro','con','draw')"),
    sa.CheckConstraint('con_clarity BETWEEN 0 AND 15'),
    sa.CheckConstraint('con_logical_reasoning BETWEEN 0 AND 35'),
    sa.CheckConstraint('con_rebuttal_quality BETWEEN 0 AND 25'),
    sa.CheckConstraint('con_relevance BETWEEN 0 AND 25'),
    sa.CheckConstraint('pro_clarity BETWEEN 0 AND 15'),
    sa.CheckConstraint('pro_logical_reasoning BETWEEN 0 AND 35'),
    sa.CheckConstraint('pro_rebuttal_quality BETWEEN 0 AND 25'),
    sa.CheckConstraint('pro_relevance BETWEEN 0 AND 25'),
    sa.ForeignKeyConstraint(['debate_id'], ['debates.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('debate_id')
    )
    op.create_table('provider_call_reservations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('debate_id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('stage', sa.String(), nullable=False),
    sa.Column('reserved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('model_id', sa.String(), nullable=False),
    sa.Column('price_version', sa.String(), nullable=False),
    sa.Column('input_token_cap', sa.Integer(), nullable=False),
    sa.Column('output_token_cap', sa.Integer(), nullable=False),
    sa.Column('reserved_max_cost_cents', sa.Integer(), nullable=False),
    sa.Column('dispatch_state', sa.String(), nullable=False),
    sa.Column('actual_input_tokens', sa.Integer(), nullable=True),
    sa.Column('actual_output_tokens', sa.Integer(), nullable=True),
    sa.Column('actual_cost_cents', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("dispatch_state IN ('reserved','dispatched','committed','failed','abandoned')"),
    sa.ForeignKeyConstraint(['debate_id'], ['debates.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['execution_attempts.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('provider_call_reservations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_provider_call_reservations_created_at'), ['created_at'], unique=False)



def downgrade():
    with op.batch_alter_table('provider_call_reservations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_provider_call_reservations_created_at'))

    op.drop_table('provider_call_reservations')
    op.drop_table('judgments')
    with op.batch_alter_table('execution_attempts', schema=None) as batch_op:
        batch_op.drop_index('ix_attempts_debate_status')

    op.drop_table('execution_attempts')
    op.drop_table('debate_messages')
    with op.batch_alter_table('creation_quota_records', schema=None) as batch_op:
        batch_op.drop_index('ix_creations_bucket_time')
        batch_op.drop_index(batch_op.f('ix_creation_quota_records_created_at'))

    op.drop_table('creation_quota_records')
    with op.batch_alter_table('debates', schema=None) as batch_op:
        batch_op.drop_index('ix_debates_history')

    op.drop_table('debates')
