"""Tests for jupyterhub.reconciler.orphan_reconcile."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from jupyterhub.orm import Base, Phase, Spawner
from jupyterhub.reconciler import _ANN, orphan_reconcile


@pytest.fixture
def Session():
    # StaticPool: 모든 session 이 같은 in-memory DB(단일 connection)를 공유.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _pod(name, sid):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, annotations={_ANN: str(sid)})
    )


async def test_orphan_reconcile_deletes_orphan_and_marks_missing(Session):
    s = Session()
    s.add_all(
        [
            Spawner(id=10, name="x", phase=Phase.STOPPED),  # pod 존재 -> orphan
            Spawner(id=11, name="y", phase=Phase.RUNNING),  # pod 없음 -> failed
            Spawner(id=12, name="z", phase=Phase.RUNNING),  # pod 존재 -> 유지
        ]
    )
    s.commit()
    s.close()

    api = MagicMock()
    api.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_pod("jupyter-10", 10), _pod("jupyter-12", 12)])
    )
    api.delete_namespaced_pod = AsyncMock()

    result = await orphan_reconcile(Session, api, "ns")

    assert result == {"orphan_deleted": 1, "marked_failed": 1}
    api.delete_namespaced_pod.assert_awaited_once_with("jupyter-10", "ns")
    chk = Session()
    assert chk.get(Spawner, 11).phase == Phase.FAILED
    assert chk.get(Spawner, 12).phase == Phase.RUNNING
    assert chk.get(Spawner, 10).phase == Phase.STOPPED
    chk.close()
