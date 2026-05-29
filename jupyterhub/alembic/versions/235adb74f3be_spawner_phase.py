"""add spawner phase column

Spawner lifecycle 를 DB 로 외부화하기 위한 phase 컬럼을 추가한다. multi-replica hub 가
이 컬럼을 single source of truth 로 삼아 상태 판단·takeover·cull 을 한다. 기존 row 는
server_default 로 'stopped' 가 된다.

Revision ID: 235adb74f3be
Revises: 4621fec11365
Create Date: 2026-05-29 14:30:00.000000

"""

# revision identifiers, used by Alembic.
revision = '235adb74f3be'
down_revision = '4621fec11365'
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade():
    op.add_column(
        'spawners',
        sa.Column(
            'phase',
            sa.Unicode(16),
            nullable=False,
            server_default='stopped',
        ),
    )
    op.create_index('ix_spawners_phase', 'spawners', ['phase'])


def downgrade():
    op.drop_index('ix_spawners_phase', table_name='spawners')
    op.drop_column('spawners', 'phase')
