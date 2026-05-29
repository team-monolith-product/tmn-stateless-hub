"""HubLeader: k8s Lease 기반 단일 leader 선출 + 정기 callback registry.

multi-replica hub 에서 cluster-wide 단발성 작업(idle cull, orphan reconcile 등)은 분산할
가치가 없고 동시 실행 시 충돌만 난다. 그래서 `coordination.k8s.io/Lease` 로 replica 중 한 명을
leader 로 뽑아 그 replica 에서만 등록된 callback 을 돌린다. kube-controller-manager 와 같은
표준 패턴이라 `kubectl get lease` 로 관찰 가능하다.

INF-232 에서는 idle cull 과 orphan reconcile 을 등록한다. 이후 task(token purge, last_activity
flush 등)는 `register(callback, interval_s)` 한 줄로 추가한다.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException


def _utcnow():
    return datetime.now(timezone.utc)


class HubLeader:
    def __init__(
        self,
        coordination_api,
        namespace,
        holder_identity,
        *,
        lease_name="jupyterhub-hub-leader",
        lease_duration_s=30,
        renew_interval_s=10,
        log=None,
        now=_utcnow,
    ):
        self.api = coordination_api
        self.namespace = namespace
        self.identity = holder_identity
        self.lease_name = lease_name
        self.lease_duration_s = lease_duration_s
        self.renew_interval_s = renew_interval_s
        self.log = log or logging.getLogger("hub_leader")
        self._now = now

        self.is_leader = False
        self._tasks = []  # list[(callback, interval_s)]
        self._handles = []  # list[asyncio.Task]

    def register(self, callback, interval_s):
        """leader 상승 시 interval_s 주기로 실행될 callback 을 등록한다."""
        self._tasks.append((callback, interval_s))

    async def run(self):
        """acquire/renew 루프. leader 전이 시 등록 callback 을 시작/중지한다."""
        try:
            while True:
                acquired = await self._acquire_or_renew()
                if acquired and not self.is_leader:
                    self.is_leader = True
                    self.log.info("Became hub-leader (%s)", self.identity)
                    self._start_tasks()
                elif not acquired and self.is_leader:
                    self.is_leader = False
                    self.log.info("Lost hub-leader (%s)", self.identity)
                    self._stop_tasks()
                await asyncio.sleep(self.renew_interval_s)
        finally:
            if self.is_leader:
                self._stop_tasks()

    async def _acquire_or_renew(self):
        """Lease 를 잡거나 갱신한다. 성공 시 True(=leader)."""
        try:
            lease = await self.api.read_namespaced_lease(
                self.lease_name, self.namespace
            )
        except ApiException as e:
            if e.status == 404:
                return await self._create_lease()
            raise

        spec = lease.spec
        if spec.holder_identity == self.identity:
            return await self._renew_lease(lease)

        # 다른 holder. 만료됐으면 takeover.
        if self._is_expired(spec):
            return await self._renew_lease(lease, takeover=True)
        return False

    def _is_expired(self, spec):
        if spec.renew_time is None:
            return True
        age = self._now() - spec.renew_time
        return age > timedelta(seconds=self.lease_duration_s)

    async def _create_lease(self):
        now = self._now()
        lease = client.V1Lease(
            metadata=client.V1ObjectMeta(
                name=self.lease_name, namespace=self.namespace
            ),
            spec=client.V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration_s,
                acquire_time=now,
                renew_time=now,
                lease_transitions=0,
            ),
        )
        try:
            await self.api.create_namespaced_lease(self.namespace, lease)
            return True
        except ApiException as e:
            if e.status == 409:
                # 다른 replica 가 먼저 만들었다. 이번 라운드는 leader 아님.
                return False
            raise

    async def _renew_lease(self, lease, takeover=False):
        now = self._now()
        lease.spec.holder_identity = self.identity
        lease.spec.lease_duration_seconds = self.lease_duration_s
        lease.spec.renew_time = now
        if takeover:
            lease.spec.acquire_time = now
            lease.spec.lease_transitions = (lease.spec.lease_transitions or 0) + 1
        try:
            # resourceVersion 기반 optimistic concurrency: 다른 replica 가 그새 갱신했으면 409.
            await self.api.replace_namespaced_lease(
                self.lease_name, self.namespace, lease
            )
            return True
        except ApiException as e:
            if e.status == 409:
                return False
            raise

    def _start_tasks(self):
        for callback, interval_s in self._tasks:
            self._handles.append(
                asyncio.ensure_future(self._run_periodic(callback, interval_s))
            )

    def _stop_tasks(self):
        for h in self._handles:
            h.cancel()
        self._handles = []

    async def _run_periodic(self, callback, interval_s):
        while True:
            try:
                await callback()
            except Exception:
                self.log.exception("hub-leader callback failed; will retry next interval")
            await asyncio.sleep(interval_s)
