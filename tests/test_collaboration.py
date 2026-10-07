import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.audit import AuditRecorder
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S9', 'start_km': 200.0, 'end_km': 210.0, 'depth_m': 900.0, 'sea_state': 2, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 100}


class FakeClock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


JIA_RM = lambda uid="jia-rm": Actor(uid, "repair_manager", "JIA")
YI_RM = Actor("yi-rm", "repair_manager", "YI")
YI_VM = Actor("yi-vm", "vessel_master", "YI")
JIA_VM = Actor("jia-vm", "vessel_master", "JIA")
YI_NOC = Actor("yi-noc", "noc_operator", "YI")
ADMIN = Actor("dispatch", "admin", "HQ")


class CollaborationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
        repository = Repository(str(Path(self.temp.name) / "test.db"))
        self.service = Service(repository, DomainRules(), AuditRecorder(repository), clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def iso(self, hours_from_now):
        return (self.clock.moment + timedelta(hours=hours_from_now)).isoformat()

    def create_record(self):
        return self.service.create(Actor("jia-noc", "noc_operator", "JIA"), "CABLE-90001", CREATE_DATA)

    def grant(self, record, stage="approve", target="YI", actor=None, hours=24, reason="乙分局就近支援"):
        return self.service.grant_delegation(
            actor or JIA_RM(), record["id"], record["version"],
            {"target_org": target, "stage": stage, "valid_from": self.iso(0),
             "valid_until": self.iso(hours), "reason": reason},
        )

    # ---- 可见性：本分局与当前受托分局 ----

    def test_owner_and_trustee_visibility(self):
        record = self.create_record()
        with self.assertRaises(PermissionDenied):
            self.service.get_record(Actor("bing-noc", "noc_operator", "BING"), record["id"])
        record = self.grant(record)
        detail = self.service.get_record(YI_RM, record["id"])
        self.assertEqual(detail["owner_org"], "JIA")
        self.assertEqual(detail["trustee_org"], "YI")
        self.assertEqual(detail["writer_org"], "YI")
        # 受托方在列表中也能看到
        self.assertEqual(1, len(self.service.list_records(YI_NOC)))
        self.assertEqual(1, len(self.service.list_records(ADMIN)))

    # ---- 委托：按阶段、生效期、唯一写入分局 ----

    def test_delegated_stage_writer(self):
        record = self.grant(self.create_record(), stage="approve")
        # 管辖分局在该阶段写入被拒，被告知受托方与冲突动作
        with self.assertRaises(Conflict) as caught:
            self.service.act(JIA_RM(), record["id"], record["version"], "approve", {"repair_manager": "RM-J"})
        self.assertEqual(caught.exception.details["writer_org"], "YI")
        self.assertEqual(caught.exception.details["conflict_action"], "approve")
        # 受托方按阶段写入成功
        record = self.service.act(YI_RM, record["id"], record["version"], "approve", {"repair_manager": "RM-Y"})
        self.assertEqual(record["state"], "approved")

    def test_trustee_limited_to_delegated_stage(self):
        record = self.grant(self.create_record(), stage="approve")
        self.service.act(YI_RM, record["id"], record["version"], "approve", {"repair_manager": "RM-Y"})
        record = self.service.get_record(YI_RM, record["id"])
        # 受托只覆盖批准阶段，动员动作无权办理
        with self.assertRaises(PermissionDenied):
            self.service.act(YI_VM, record["id"], record["version"], "mobilize",
                             {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-Y"})
        # 委托结束后管辖分局恢复写入（退回返回交接回执）
        receipt = self.service.return_delegation(
            YI_RM, record["id"], record["version"], {"reason": "阶段完成，工单交回"})
        self.assertEqual(receipt["receipt"]["details"]["reason"], "阶段完成，工单交回")
        record = self.service.get_record(JIA_RM(), record["id"])
        record = self.service.act(JIA_VM, record["id"], record["version"], "mobilize",
                                  {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-J"})
        self.assertEqual(record["state"], "mobilized")

    def test_only_manager_of_owner_can_grant(self):
        record = self.create_record()
        with self.assertRaises(PermissionDenied):
            self.grant(record, actor=YI_RM)
        with self.assertRaises(PermissionDenied):
            self.grant(record, actor=Actor("jia-vm", "vessel_master", "JIA"))
        with self.assertRaises(PermissionDenied):
            self.grant(record, target="JIA")

    def test_single_active_delegation(self):
        record = self.grant(self.create_record())
        with self.assertRaises(Conflict):
            self.grant(record, target="BING")

    # ---- 退回 / 上级撤回 / 到期：立即失效 ----

    def test_return_revokes_immediately(self):
        record = self.grant(self.create_record())
        receipt = self.service.return_delegation(
            YI_RM, record["id"], record["version"], {"reason": "无船机档期"})
        self.assertEqual("JIA", receipt["writer_org"])
        self.assertEqual("", receipt["trustee_org"])
        with self.assertRaises(PermissionDenied):
            self.service.act(YI_RM, record["id"], receipt["version"], "approve", {"repair_manager": "RM-Y"})
        record = self.service.get_record(JIA_RM(), record["id"])
        with self.assertRaises(Conflict):
            self.service.return_delegation(YI_RM, record["id"], record["version"], {"reason": "重复退回"})

    def test_superior_withdraw(self):
        record = self.grant(self.create_record())
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_delegation(JIA_RM(), record["id"], record["version"], {"reason": "管辖经理不可撤回"})
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_delegation(YI_RM, record["id"], record["version"], {"reason": "受托不可撤回"})
        record = self.service.withdraw_delegation(ADMIN, record["id"], record["version"], {"reason": "调度改派"})
        self.assertEqual("JIA", record["writer_org"])

    def test_expiry_revokes_immediately(self):
        record = self.grant(self.create_record(), hours=4)
        self.clock.advance(hours=5)
        # 到期立即失效：受托方看不到也写不了，管辖分局恢复
        with self.assertRaises(PermissionDenied):
            self.service.get_record(YI_RM, record["id"])
        detail = self.service.get_record(JIA_RM(), record["id"])
        self.assertEqual("", detail["trustee_org"])
        self.assertEqual("JIA", detail["writer_org"])
        with self.assertRaises(PermissionDenied):
            self.service.act(YI_RM, detail["id"], detail["version"], "approve", {"repair_manager": "RM-Y"})
        actions = [event["action"] for event in self.service.timeline(JIA_RM(), detail["id"])]
        self.assertIn("delegation_expired", actions)

    def test_future_valid_from_not_yet_effective(self):
        record = self.create_record()
        record = self.service.grant_delegation(
            JIA_RM(), record["id"], record["version"],
            {"target_org": "YI", "stage": "approve", "valid_from": self.iso(2),
             "valid_until": self.iso(10), "reason": "两小时后接手"})
        # 未到生效时间：管辖分局仍可写入，受托方尚不可写
        self.assertEqual("JIA", record["writer_org"])
        with self.assertRaises(PermissionDenied):
            self.service.act(YI_RM, record["id"], record["version"], "approve", {"repair_manager": "RM-Y"})
        record = self.service.act(JIA_RM(), record["id"], record["version"], "approve", {"repair_manager": "RM-J"})
        self.assertEqual("approved", record["state"])

    # ---- 并发：只一方写入，另一方看到受托方与冲突动作，可按新版本重试 ----

    def test_concurrent_write_only_one_wins(self):
        record = self.grant(self.create_record())
        v = record["version"]
        # 两个分局几乎同时写入：乙先成功，甲（同受托场景改为乙内部两客户端）失败
        winner = self.service.act(YI_RM, record["id"], v, "approve", {"repair_manager": "RM-Y1"})
        with self.assertRaises(Conflict) as caught:
            self.service.act(Actor("yi-rm-2", "repair_manager", "YI"), record["id"], v, "approve",
                             {"repair_manager": "RM-Y2"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.details["current_version"], winner["version"])
        self.assertEqual(caught.exception.details["writer_org"], "YI")
        self.assertEqual(caught.exception.details["winning_action"], "approve")
        # 原记录保留（状态未被第二个请求改动）
        self.assertEqual("approved", self.service.get_record(YI_RM, record["id"])["state"])
        # 冲突凭据可在详情页看到
        detail = self.service.get_record(YI_RM, record["id"])
        self.assertEqual(1, len(detail["conflicts"]))
        self.assertEqual("write_conflict", detail["conflicts"][0]["action"])
        # 换手失败后按刷新得到的新版本重试（approve 已不可用，改用全流程委托演示重试语义）
        # 此处验证：旧版本被拒后，客户端用最新版本重发同一动作会得到“状态不允许”，而非版本冲突
        with self.assertRaises(Conflict) as retry:
            self.service.act(Actor("yi-rm-2", "repair_manager", "YI"), record["id"],
                             winner["version"], "approve", {"repair_manager": "RM-Y2"})
        self.assertNotIn("版本冲突", str(retry.exception))

    def test_concurrent_delegation_handover_conflict(self):
        record = self.create_record()
        v = record["version"]
        self.grant(record, target="YI")
        # 甲基于旧版本发起改派给丙 -> 版本冲突
        with self.assertRaises(Conflict):
            self.service.grant_delegation(
                JIA_RM(), record["id"], v,
                {"target_org": "BING", "stage": "approve", "valid_from": self.iso(0),
                 "valid_until": self.iso(8), "reason": "基于旧版本改派"})
        detail = self.service.get_record(JIA_RM(), record["id"])
        self.assertEqual(1, len(detail["conflicts"]))

    # ---- 详情页：管辖、受托与交接 ----

    def test_detail_shows_owner_trustee_handovers(self):
        record = self.grant(self.create_record(), stage="all", hours=24)
        self.service.act(YI_RM, record["id"], record["version"], "approve", {"repair_manager": "RM-Y"})
        record = self.service.get_record(YI_RM, record["id"])
        record = self.service.return_delegation(YI_RM, record["id"], record["version"], {"reason": "交回甲"})
        detail = self.service.get_record(JIA_RM(), record["id"])
        self.assertEqual("JIA", detail["owner_org"])
        delegations = detail["delegations"]
        self.assertEqual("returned", delegations[-1]["status"])
        handover_actions = [event["action"] for event in detail["handovers"]]
        self.assertEqual(["delegation_granted", "delegation_returned"], handover_actions)

    def test_delegation_does_not_leak_after_return(self):
        record = self.grant(self.create_record())
        record = self.service.return_delegation(YI_RM, record["id"], record["version"], {"reason": "交回"})
        with self.assertRaises(PermissionDenied):
            self.service.get_record(YI_RM, record["id"])
        self.assertEqual([], self.service.list_records(YI_RM))


if __name__ == "__main__":
    unittest.main()
