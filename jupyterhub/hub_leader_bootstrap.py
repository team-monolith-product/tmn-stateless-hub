"""hub IOLoop 에 HubLeader 를 기동하는 bootstrap.

helm 의 jupyterhub_config(index.py)에서 `start_hub_leader(...)` 한 줄로 호출한다. leader/cull
에 필요한 자원(DB 세션 팩토리, k8s client)을 조립하고 hub IOLoop 시작 직후 leader 루프를 띄운다.

callback 등록 로직(build_leader)은 단위 테스트하고, in-cluster config 로드·IOLoop 기동
(start_hub_leader)은 dev 클러스터에서 검증한다.
"""

import uuid
from functools import partial

from .hub_leader import HubLeader
from .reconciler import orphan_reconcile


def build_leader(
    coordination_api,
    core_api,
    session_factory,
    namespace,
    pod_name,
    *,
    reconcile_interval_s=300,
    label_selector="component=singleuser-server",
    lease_name="jupyterhub-hub-leader",
):
    """orphan_reconcile 을 등록한 HubLeader 를 만든다(기동은 하지 않음).

    idle culling 은 본 task scope 밖이다. 후속 task 에서 leader.register(callback, interval)
    한 줄로 추가한다.
    """
    leader = HubLeader(
        coordination_api,
        namespace,
        f"{pod_name}-{uuid.uuid4().hex[:8]}",
        lease_name=lease_name,
    )
    leader.register(
        partial(
            orphan_reconcile,
            session_factory,
            core_api,
            namespace,
            label_selector=label_selector,
        ),
        reconcile_interval_s,
    )
    return leader


def start_hub_leader(namespace, pod_name, **kwargs):
    """hub IOLoop 시작 직후 HubLeader 를 비동기로 기동한다. config 평가 시점에 호출한다.

    tornado 는 IOLoop 시작 전 add_callback 을 큐잉했다가 loop 시작 후 실행한다.

    db_url 은 인자로 받지 않고 _run() 에서 JupyterHub.instance() 로 읽는다. z2jh 는
    jupyterhub_config.d(이 함수의 호출 지점)를 extraConfig(db_url 을 set 하는 스니펫)보다
    먼저 로드하므로, config 평가 시점의 c.JupyterHub.db_url 은 LazyConfigValue 다. IOLoop
    콜백 시점에는 app.initialize() 가 끝나 instance().db_url 이 항상 실제 값이다.
    """
    import asyncio

    from tornado.ioloop import IOLoop

    async def _run():
        from kubernetes_asyncio import client, config
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from jupyterhub.app import JupyterHub

        db_url = JupyterHub.instance().db_url

        # leader/cull 전용 sync 엔진. hub 의 메인 세션과 분리한다. 백그라운드 루프라 작은 풀로 충분.
        engine = create_engine(db_url, pool_size=2, max_overflow=2, pool_pre_ping=True)
        session_factory = sessionmaker(bind=engine)

        config.load_incluster_config()
        leader = build_leader(
            client.CoordinationV1Api(),
            client.CoreV1Api(),
            session_factory,
            namespace,
            pod_name,
            **kwargs,
        )
        await leader.run()

    IOLoop.current().add_callback(lambda: asyncio.ensure_future(_run()))
