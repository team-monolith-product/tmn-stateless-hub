"""hub-leader 가 단독 실행하는 cluster-wide 정기 작업: idle cull, orphan reconcile.

DB(`spawners.phase` + `last_activity`)를 기준으로 동작한다. leader 한 명만 실행하므로 행 잠금
(SKIP LOCKED)은 필요 없다. Pod 삭제와 phase 변경만 하고, hub 의 in-memory state·CHP route 정리는
각 replica 의 `DBSpawner.poll()` 이 DB phase 를 읽어 자연히 수렴시킨다.
"""

import logging
from datetime import timedelta

from kubernetes_asyncio.client.rest import ApiException

from jupyterhub.db_spawner import DBSpawner
from jupyterhub.orm import Phase, Spawner
from jupyterhub.utils import utcnow

_ANN = DBSpawner.SPAWNER_ID_ANNOTATION
# 살아있다고 보는(=Pod 가 있어야 하는) phase 들.
_ACTIVE = (Phase.PENDING, Phase.STARTING, Phase.RUNNING)


async def _delete_pod(k8s_api, namespace, pod_name, log):
    try:
        await k8s_api.delete_namespaced_pod(pod_name, namespace)
    except ApiException as e:
        if e.status != 404:  # 404 = 이미 없음 = 성공
            log.warning("Failed to delete pod %s: %s", pod_name, e)


def _set_phase(session_factory, spawner_id, phase):
    session = session_factory()
    try:
        row = session.get(Spawner, spawner_id)
        if row is not None:
            row.phase = phase
            session.commit()
    finally:
        session.close()


async def idle_cull(
    session_factory, k8s_api, namespace, cull_timeout_s, *, batch=100, log=None
):
    """last_activity 가 cull_timeout 을 넘긴 running spawner 의 Pod 를 정리한다."""
    log = log or logging.getLogger("idle_culler")
    # last_activity 는 naive UTC 로 저장되므로(utcnow(with_tz=False)) cutoff 도 naive 로 맞춘다.
    cutoff = utcnow(with_tz=False) - timedelta(seconds=cull_timeout_s)

    session = session_factory()
    targets = []
    try:
        rows = (
            session.query(Spawner)
            .filter(
                Spawner.phase == Phase.RUNNING,
                Spawner.last_activity.isnot(None),
                Spawner.last_activity < cutoff,
            )
            .order_by(Spawner.last_activity)
            .limit(batch)
            .all()
        )
        for r in rows:
            targets.append((r.id, (r.state or {}).get("pod_name")))
            r.phase = Phase.STOPPING
        session.commit()
    finally:
        session.close()

    # DB 트랜잭션을 닫은 뒤 K8s DELETE.
    for spawner_id, pod_name in targets:
        if pod_name:
            await _delete_pod(k8s_api, namespace, pod_name, log)
        else:
            log.warning("spawner id=%s has no pod_name in state; skipping pod delete", spawner_id)
        _set_phase(session_factory, spawner_id, Phase.STOPPED)

    if targets:
        log.info("idle_cull stopped %d server(s)", len(targets))
    return len(targets)


async def orphan_reconcile(
    session_factory, k8s_api, namespace, *, label_selector="component=singleuser-server", log=None
):
    """Pod 와 DB phase 의 불일치를 보정한다.

    - Pod 는 있는데 DB 가 active 로 보지 않으면 orphan 으로 보고 Pod 삭제.
    - DB 가 running 인데 Pod 가 없으면 failed 로 표시.
    in-flight(pending/starting) 행은 건드리지 않는다(Pod 생성 중일 수 있으므로).
    """
    log = log or logging.getLogger("idle_culler")
    pod_list = await k8s_api.list_namespaced_pod(namespace, label_selector=label_selector)

    # 우리 annotation 이 있는 Pod 만 대상. (구 KubeSpawner Pod 는 annotation 없으면 제외)
    pod_by_sid = {}
    for pod in pod_list.items:
        ann = pod.metadata.annotations or {}
        sid = ann.get(_ANN)
        if sid is not None:
            pod_by_sid[sid] = pod.metadata.name

    session = session_factory()
    try:
        active = (
            session.query(Spawner).filter(Spawner.phase.in_(_ACTIVE)).all()
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
