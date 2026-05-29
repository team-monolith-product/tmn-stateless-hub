"""hub-leader 가 단독 실행하는 orphan reconcile.

DBSpawner.poll() 은 DB `phase` 를 신뢰하므로, 죽은 Pod(=DB 는 running 인데 Pod 없음)의 phase
보정과 orphan Pod(=Pod 는 있는데 DB 가 active 로 안 봄) 정리는 leader 한 명이 주기적으로 한다.
idle culling(저해요소 4)은 본 task scope 밖이며 별도 task 에서 HubLeader 에 callback 으로 추가한다.
"""

import logging

from kubernetes_asyncio.client.rest import ApiException
from sqlalchemy.orm import undefer

from jupyterhub.db_spawner import DBSpawner
from jupyterhub.orm import Phase, Spawner

_ANN = DBSpawner.SPAWNER_ID_ANNOTATION
# Pod 가 있어야 하는(=살아있다고 보는) phase 들.
_ACTIVE = (Phase.PENDING, Phase.STARTING, Phase.RUNNING)


async def _delete_pod(k8s_api, namespace, pod_name, log):
    try:
        await k8s_api.delete_namespaced_pod(pod_name, namespace)
    except ApiException as e:
        if e.status != 404:  # 404 = 이미 없음 = 성공
            log.warning("Failed to delete pod %s: %s", pod_name, e)


async def orphan_reconcile(
    session_factory,
    k8s_api,
    namespace,
    *,
    label_selector="component=singleuser-server",
    log=None,
):
    """Pod 와 DB phase 의 불일치를 보정한다.

    - Pod 는 있는데 DB 가 active 로 보지 않으면 orphan 으로 보고 Pod 삭제.
    - DB 가 running 인데 Pod 가 없으면 failed 로 표시.
    in-flight(pending/starting) 행은 Pod 생성 중일 수 있으므로 건드리지 않는다.
    """
    log = log or logging.getLogger("reconciler")
    pod_list = await k8s_api.list_namespaced_pod(namespace, label_selector=label_selector)

    pod_by_sid = {}
    for pod in pod_list.items:
        ann = pod.metadata.annotations or {}
        sid = ann.get(_ANN)
        if sid is not None:
            pod_by_sid[sid] = pod.metadata.name

    session = session_factory()
    try:
        # phase 는 deferred 라 row 별 접근 시 N+1 이 되므로 undefer 로 한 번에 끌어온다.
        active = (
            session.query(Spawner)
            .options(undefer(Spawner.phase))
            .filter(Spawner.phase.in_(_ACTIVE))
            .all()
        )
        active_ids = {str(r.id) for r in active}
        running_ids = {str(r.id) for r in active if r.phase == Phase.RUNNING}

        orphan_pods = [
            (sid, name) for sid, name in pod_by_sid.items() if sid not in active_ids
        ]
        missing = [int(rid) for rid in running_ids if rid not in pod_by_sid]
        for rid in missing:
            row = session.get(Spawner, rid)
            if row is not None:
                row.phase = Phase.FAILED
        session.commit()
    finally:
        session.close()

    for sid, name in orphan_pods:
        await _delete_pod(k8s_api, namespace, name, log)

    if orphan_pods or missing:
        log.info(
            "orphan_reconcile: deleted %d orphan pod(s), marked %d running->failed",
            len(orphan_pods),
            len(missing),
        )
    return {"orphan_deleted": len(orphan_pods), "marked_failed": len(missing)}
