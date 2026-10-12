"""initial schema

Revision ID: 0001
Revises:

Fresh baseline of the full schema.
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_setting',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('household',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('banned_term',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('household_id', sa.Text(), nullable=False),
    sa.Column('term', sa.Text(), nullable=False),
    sa.Column('replacement', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('household_id', 'term', name='uq_banned_household_term')
    )
    op.create_table('child',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('household_id', sa.Text(), nullable=False),
    sa.Column('public_slug', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('age_band', sa.Text(), nullable=False),
    sa.Column('pronouns', sa.Text(), nullable=True),
    sa.Column('active', sa.Integer(), nullable=False),
    sa.Column('length_override', sa.Text(), nullable=True),
    sa.Column('narrator_voice', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('language', sa.Text(), nullable=False, server_default=sa.text("'en'")),
    sa.Column('world_bible', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.CheckConstraint("age_band IN ('early','older')", name='ck_child_age_band'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
    sa.ForeignKeyConstraint(['narrator_voice'], ['voice_profile.id'], name='fk_child_narrator_voice', use_alter=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_slug')
    )
    op.create_table('parent',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('household_id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('external_subject', sa.Text(), nullable=True),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('is_admin', sa.Integer(), nullable=False, server_default=sa.text("'0'")),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('pronunciation',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('household_id', sa.Text(), nullable=False),
    sa.Column('surface', sa.Text(), nullable=False),
    sa.Column('ipa', sa.Text(), nullable=True),
    sa.Column('respelling', sa.Text(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('household_id', 'surface', name='uq_pron_household_surface')
    )
    op.create_table('voice_profile',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('household_id', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('engine', sa.Text(), nullable=False),
    sa.Column('voice_id', sa.Text(), nullable=True),
    sa.Column('ref_audio_path', sa.Text(), nullable=True),
    sa.Column('consent_by', sa.Text(), nullable=True),
    sa.Column('consent_at', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.CheckConstraint("kind IN ('builtin','clone')", name='ck_voice_kind'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('character',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('child_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('archetype', sa.Text(), nullable=True),
    sa.Column('linked_child_id', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['child_id'], ['child.id'], ),
    sa.ForeignKeyConstraint(['linked_child_id'], ['child.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('session',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('parent_id', sa.Text(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['parent.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('story_request',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('child_id', sa.Text(), nullable=False),
    sa.Column('story_type', sa.Text(), nullable=False),
    sa.Column('character_ids', sa.Text(), nullable=False),
    sa.Column('special_request', sa.Text(), nullable=True),
    sa.Column('target_date', sa.Text(), nullable=True),
    sa.Column('priority', sa.Text(), nullable=False),
    sa.Column('gen_model_override', sa.Text(), nullable=True),
    sa.Column('continue_series', sa.Integer(), nullable=False, server_default=sa.text("'0'")),
    sa.Column('long_form', sa.Integer(), nullable=False, server_default=sa.text("'0'")),
    sa.Column('ambient', sa.Text(), nullable=False, server_default=sa.text("'off'")),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['child_id'], ['child.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('story',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('child_id', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('age_band', sa.Text(), nullable=False),
    sa.Column('story_json', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('word_count', sa.Integer(), nullable=True),
    sa.Column('gen_model', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False, server_default=sa.text("'short'")),
    sa.Column('ambient', sa.Text(), nullable=False, server_default=sa.text("'off'")),
    sa.Column('language', sa.Text(), nullable=False, server_default=sa.text("'en'")),
    sa.Column('stage', sa.Text(), nullable=True),
    sa.Column('generation_seconds', sa.REAL(), nullable=True),
    sa.Column('gen_step_seconds', sa.REAL(), nullable=True),
    sa.Column('rating', sa.Integer(), nullable=True),
    sa.Column('rating_note', sa.Text(), nullable=True),
    sa.Column('reject_reason', sa.Text(), nullable=True),
    sa.Column('reject_note', sa.Text(), nullable=True),
    sa.Column('play_count', sa.Integer(), nullable=False, server_default=sa.text("'0'")),
    sa.Column('favorited', sa.Boolean(), nullable=False, server_default=sa.text("'0'")),
    sa.Column('craft_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['child_id'], ['child.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['story_request.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('playback_event',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('child_id', sa.Text(), nullable=False),
    sa.Column('story_id', sa.Text(), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('position_seconds', sa.REAL(), nullable=True),
    sa.Column('percent', sa.REAL(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['child_id'], ['child.id'], ),
    sa.ForeignKeyConstraint(['story_id'], ['story.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('safety_review',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('story_id', sa.Text(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('categories', sa.Text(), nullable=False),
    sa.Column('reasons', sa.Text(), nullable=True),
    sa.Column('banned_hits', sa.Text(), nullable=True),
    sa.Column('judge_model', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['story_id'], ['story.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('story_asset',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('story_id', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('duration_seconds', sa.REAL(), nullable=True),
    sa.Column('voice', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['story_id'], ['story.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('story_embedding',
    sa.Column('story_id', sa.Text(), nullable=False),
    sa.Column('child_id', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('vec', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['child_id'], ['child.id'], ),
    sa.ForeignKeyConstraint(['story_id'], ['story.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('story_id')
    )
    with op.batch_alter_table('story_embedding', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_story_embedding_child_id'), ['child_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('story_embedding', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_story_embedding_child_id'))

    op.drop_table('story_embedding')
    op.drop_table('story_asset')
    op.drop_table('safety_review')
    op.drop_table('playback_event')
    op.drop_table('story')
    op.drop_table('story_request')
    op.drop_table('session')
    op.drop_table('character')
    op.drop_table('voice_profile')
    op.drop_table('pronunciation')
    op.drop_table('parent')
    op.drop_table('child')
    op.drop_table('banned_term')
    op.drop_table('household')
    op.drop_table('app_setting')
