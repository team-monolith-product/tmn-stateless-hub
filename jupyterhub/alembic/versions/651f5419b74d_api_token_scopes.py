"""api_token_scopes

Revision ID: 651f5419b74d
Revises: 833da8570507
Create Date: 2022-02-28 12:42:55.149046

"""

# revision identifiers, used by Alembic.
revision = '651f5419b74d'
down_revision = '833da8570507'
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, ForeignKey, Table, text
from sqlalchemy.orm import raiseload, relationship, selectinload
from sqlalchemy.orm.session import Session

from jupyterhub import orm, roles


def access_scopes(oauth_client: orm.OAuthClient, db: Session):
    """Return scope(s) required to access an oauth client
    This is a clone of `scopes.access_scopes` without using
    the `orm.Service`
    """
    scopes = set()
    if oauth_client.identifier == "jupyterhub":
        return frozenset()
    # INF-232: spawner 조회를 raw SQL 로 한다. ORM 으로 oauth_client.spawner 를 로드하면
    # head 마이그레이션에서 추가된 spawners.phase 가 SELECT 에 포함돼, 이 시점 DB 에 아직
    # 없는 컬럼으로 UndefinedColumn 이 난다. 아래 services 조회와 동일한 raw SQL 패턴.
    spawner_row = db.execute(
        text(
            "SELECT s.name AS server_name, u.name AS user_name "
            "FROM spawners s JOIN users u ON u.id = s.user_id "
            "WHERE s.oauth_client_id = :identifier"
        ),
        {"identifier": oauth_client.identifier},
    ).fetchone()
    if spawner_row:
        scopes.add(
            f"access:servers!server={spawner_row.user_name}/{spawner_row.server_name}"
        )
    else:
        statement = "SELECT * FROM services WHERE oauth_client_id = :identifier"
        service = db.execute(
            text(statement), {"identifier": oauth_client.identifier}
        ).fetchall()
        if len(service) > 0:
            scopes.add(f"access:services!service={service[0].name}")

    return frozenset(scopes)


def upgrade():
    c = op.get_bind()
    tables = sa.inspect(c.engine).get_table_names()

    # oauth codes are short lived, no need to upgrade them
    if 'oauth_code_role_map' in tables:
        op.drop_table('oauth_code_role_map')

    if 'oauth_codes' in tables:
        op.add_column('oauth_codes', sa.Column('scopes', orm.JSONList(), nullable=True))

    if 'api_tokens' in tables:
        # may not be present,
        # e.g. upgrade from 1.x, token table dropped
        # in which case no migration to do

        # define new scopes column on API tokens
        op.add_column('api_tokens', sa.Column('scopes', orm.JSONList(), nullable=True))

        if 'api_token_role_map' in tables:
            # redefine the to-be-removed api_token->role relationship
            # so we can run a query on it for the migration
            token_role_map = Table(
                "api_token_role_map",
                orm.Base.metadata,
                Column(
                    'api_token_id',
                    ForeignKey('api_tokens.id', ondelete='CASCADE'),
                    primary_key=True,
                ),
                Column(
                    'role_id',
                    ForeignKey('roles.id', ondelete='CASCADE'),
                    primary_key=True,
                ),
                extend_existing=True,
            )
            orm.APIToken.roles = relationship('Role', secondary='api_token_role_map')

            # tokens have roles, evaluate to scopes
            db = Session(bind=c)

            for token in db.query(orm.APIToken).options(
                selectinload(orm.APIToken.roles).defer(orm.Role.managed_by_auth),
                raiseload("*"),
            ):
                token.scopes = list(roles.roles_to_scopes(token.roles))
            db.commit()
            # drop token-role relationship
            op.drop_table('api_token_role_map')

    if 'oauth_clients' in tables:
        # define new scopes column on API tokens
        op.add_column(
            'oauth_clients', sa.Column('allowed_scopes', orm.JSONList(), nullable=True)
        )

        if 'oauth_client_role_map' in tables:
            # redefine the to-be-removed api_token->role relationship
            # so we can run a query on it for the migration
            client_role_map = Table(
                "oauth_client_role_map",
                orm.Base.metadata,
                Column(
                    'oauth_client_id',
                    ForeignKey('oauth_clients.id', ondelete='CASCADE'),
                    primary_key=True,
                ),
                Column(
                    'role_id',
                    ForeignKey('roles.id', ondelete='CASCADE'),
                    primary_key=True,
                ),
                extend_existing=True,
            )
            orm.OAuthClient.allowed_roles = relationship(
                'Role', secondary='oauth_client_role_map'
            )

            # oauth clients have allowed_roles, evaluate to allowed_scopes
            db = Session(bind=c)
            for oauth_client in db.query(orm.OAuthClient).options(
                selectinload(orm.OAuthClient.allowed_roles).defer(
                    orm.Role.managed_by_auth
                )
            ):
                allowed_scopes = set(roles.roles_to_scopes(oauth_client.allowed_roles))
                allowed_scopes.update(access_scopes(oauth_client, db))
                oauth_client.allowed_scopes = sorted(allowed_scopes)
            db.commit()
            # drop token-role relationship
            op.drop_table('oauth_client_role_map')


def downgrade():
    # cannot map permissions from scopes back to roles
    # drop whole api token table (revokes all tokens), which will be recreated on hub start
    op.drop_table('api_tokens')
    op.drop_table('oauth_clients')
    op.drop_table('oauth_codes')
