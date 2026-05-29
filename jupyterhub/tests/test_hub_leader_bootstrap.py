"""Tests for jupyterhub.hub_leader_bootstrap.build_leader.

build_leader 의 callback 등록(wiring)만 검증한다. start_hub_leader 의 in-cluster config 로드와
IOLoop 기동은 dev 클러스터에서 검증한다.
"""

from unittest.mock import MagicMock

from jupyterhub.hub_leader_bootstrap import build_leader


def test_build_leader_registers_orphan_reconcile():
    leader = build_leader(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "ns",
        "hub-0",
        reconcile_interval_s=300,
    )

    assert [interval for _, interval in leader._tasks] == [300]
    assert leader.namespace == "ns"
    assert leader.identity.startswith("hub-0-")
