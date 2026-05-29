"""Tests for jupyterhub.idle_culler (idle_cull, orphan_reconcile)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from jupyterhub.idle_culler import _ANN, idle_cull, orphan_reconcile
from jupyterhub.orm import Base, Phase, Spawner


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


async def test_idle_cull_stops_idle_running_servers(Session):
    now = datetime.utcnow()
    s = Session()
    s.add_all(
        [
            Spawner(
                id=1, name="a", phase=Phase.RUNNING,
                last_activity=now - timedelta(hours=2), state={"pod_name": "jupyter-a"},
            ),
            Spawner(
                id=2, name="b", phase=Phase.RUNNING,
                last_activity=now, state={"pod_name": "jupyter-b"},
            ),
            Spawner(id=3, name="c", phase=Phase.STOPPED, last_activity=now - timedelta(hours=5)),
        ]
    )
    s.commit()
    s.close()

    api = MagicMock()
    api.delete_namespaced_pod = AsyncMock()

    n = await idle_cull(Session, api, "ns", cull_timeout_s=3600)

    assert n == 1
    api.delete_namespaced_pod.assert_awaited_once_with("jupyter-a", "ns")
    chk = Session()
    assert chk.get(Spawner, 1).phase == Phase.STOPPED  # idle -> culled
    assert chk.get(Spawner, 2).phase == Phase.RUNNING  # 최근 활동 -> 유지
    chk.close()


async def test_orphan_reconcile_deletes_orphan_and_marks_missing(Session):
    s = Session()
    s.add_all(
        [
            Spawner(id=10, name="x", phase=Phase.STOPPED),   # pod 존재 -> orphan
            Spawner(id=11, name="y", phase=Phase.RUNNING),   # pod 없음 -> failed
            Spawner(id=12, name="z", phase=Phase.RUNNING),   # pod 존재 -> 유지
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
