"""contact form messages

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('contact_messages',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('subject', sa.String(length=150), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('sender_hash', sa.String(length=64), nullable=False),
    sa.Column('emailed', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('contact_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contact_messages_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('contact_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contact_messages_created_at'))
    op.drop_table('contact_messages')
