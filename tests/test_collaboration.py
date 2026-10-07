import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


def data(segment="S3", jurisdiction="甲分局"):
    payload = {'cable': 'SEA-1', 'segment': segment, 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
    if jurisdiction is not None:
        payload['jurisdiction_org'] = jurisdiction
    return payload


def hours(offset):
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).isoformat()


def delegation(to_org, phases, valid_from=None, valid_until=None, reason="就近支援"):
    return {'to_org': to_org, 'phases': phases, 'valid_from': valid_from or hours(-1), 'valid_until': valid_until or hours(8), 'reason': reason}


A_OP = Actor('op-a', 'noc_operator', '甲分局')
A_MGR = Actor('mgr-a', 'repair_manager', '甲分局')
A_MASTER = Actor('master-a', 'vessel_master', '甲分局')
B_ENG = Actor('eng-b', 'cable_engineer', '乙分局')
B_OP = Actor('op-b', 'noc_operator', '乙分局')
B_MGR = Actor('mgr-b', 'repair_manager', '乙分局')
C_OP = Actor('op-c', 'noc_operator', '丙分局')
C_MGR = Actor('mgr-c', 'repair_manager', '丙分局')
ADMIN = Actor('hq', 'admin', '总部')


class CollaborationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, segment="S3", jurisdiction="甲分局", actor=None):
        return self.service.create(actor or A_OP, "CABLE-%s" % segment, data(segment, jurisdiction))

    def test_jurisdiction_and_visibility(self):
        record = self.create()
        self.assertEqual(record["jurisdiction_org"], "甲分局")
        self.assertEqual(self.service.get_record(A_OP, record["id"])["id"], record["id"])
        self.assertEqual(self.service.get_record(ADMIN, record["id"])["id"], record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.get_record(B_OP, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.timeline(C_OP, record["id"])
        self.assertEqual([item["id"] for item in self.service.list_records(A_OP)], [record["id"]])
        self.assertEqual(self.service.list_records(B_OP), [])
        self.assertEqual(len(self.service.list_records(ADMIN)), 1)

    def test_jurisdiction_defaults_to_actor_org(self):
        record = self.service.create(A_OP, "CABLE-S9", data("S9", jurisdiction=None))
        self.assertEqual(record["jurisdiction_org"], "甲分局")
        legacy = self.service.create(Actor('op-x', 'noc_operator'), "CABLE-S10", data("S10", jurisdiction=None))
        self.assertEqual(legacy["jurisdiction_org"], "")
        self.assertIn(legacy["id"], [item["id"] for item in self.service.list_records(C_OP)])

    def test_cross_branch_action_denied(self):
        record = self.create()
        with self.assertRaises(PermissionDenied):
            self.service.act(B_MGR, record["id"], record["version"], 'approve', {'repair_manager': 'RM-B'})
        with self.assertRaises(PermissionDenied):
            self.service.act(C_MGR, record["id"], record["version"], 'approve', {'repair_manager': 'RM-C'})

    def test_delegate_grants_scoped_access(self):
        record = self.create()
        detail = self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        self.assertEqual(detail["version"], 1)
        self.assertEqual(detail["delegation"]["to_org"], "乙分局")
        self.assertEqual(detail["delegation"]["phases"], ["approve"])
        self.assertEqual(self.service.get_record(B_OP, record["id"])["id"], record["id"])
        detail = self.service.act(B_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-B'})
        self.assertEqual(detail["state"], "approved")
        with self.assertRaises(PermissionDenied):
            self.service.act(B_MGR, record["id"], detail["version"], 'cancel', {'cancel_reason': '越权'})
        with self.assertRaises(PermissionDenied):
            self.service.get_record(C_OP, record["id"])

    def test_entrusted_branch_phase_scope(self):
        record = self.create()
        record = self.service.act(A_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-A'})
        record = self.service.act(A_MASTER, record["id"], 2, 'mobilize', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        self.service.act(A_MGR, record["id"], 3, 'delegate', delegation('乙分局', ['survey']))
        record = self.service.act(B_ENG, record["id"], 3, 'survey', {'survey_complete': True, 'fault_location_km': 128})
        self.assertEqual(record["state"], "surveyed")
        with self.assertRaises(PermissionDenied):
            self.service.act(B_ENG, record["id"], 4, 'splice', {'splice_loss_db': 0.12, 'spare_used_km': 16})

    def test_return_invalidates_immediately(self):
        record = self.create()
        self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        detail = self.service.act(B_MGR, record["id"], 1, 'return', {'reason': '船只调度冲突'})
        self.assertIsNone(detail["delegation"])
        self.assertEqual(detail["delegations"][0]["status"], "returned")
        self.assertEqual(detail["delegations"][0]["closed_by"], "mgr-b")
        with self.assertRaises(PermissionDenied):
            self.service.get_record(B_OP, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.act(B_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-B'})

    def test_expiry_invalidates_immediately(self):
        record = self.create()
        self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve'], valid_from=hours(-3), valid_until=hours(-1)))
        with self.assertRaises(PermissionDenied):
            self.service.get_record(B_OP, record["id"])
        detail = self.service.get_record(A_OP, record["id"])
        self.assertIsNone(detail["delegation"])
        self.assertEqual(detail["delegations"][0]["status"], "expired")
        actions = [event["action"] for event in self.service.timeline(A_OP, record["id"])]
        self.assertIn("delegation_expired", actions)

    def test_revoke_only_by_supervisor(self):
        record = self.create()
        self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        with self.assertRaises(PermissionDenied):
            self.service.act(A_MGR, record["id"], 1, 'revoke', {'reason': '越权撤回'})
        detail = self.service.act(ADMIN, record["id"], 1, 'revoke', {'reason': '上级统一调度'})
        self.assertEqual(detail["delegations"][0]["status"], "revoked")
        with self.assertRaises(PermissionDenied):
            self.service.get_record(B_OP, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.act(A_MGR, record["id"], 1, 'revoke', {'reason': '再次越权'})
        with self.assertRaises(Conflict):
            self.service.act(ADMIN, record["id"], 1, 'revoke', {'reason': '无委托可撤'})

    def test_delegate_permission_rules(self):
        record = self.create()
        with self.assertRaises(PermissionDenied):
            self.service.act(A_OP, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        with self.assertRaises(PermissionDenied):
            self.service.act(B_MGR, record["id"], 1, 'delegate', delegation('丙分局', ['approve']))
        with self.assertRaises(ValidationError):
            self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('甲分局', ['approve']))
        with self.assertRaises(ValidationError):
            self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['cancel']))
        with self.assertRaises(ValidationError):
            self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve'], valid_from=hours(2), valid_until=hours(1)))
        with self.assertRaises(Conflict):
            self.service.act(B_MGR, record["id"], 1, 'return', {'reason': '无委托'})

    def test_failed_handover_retries_with_original_version(self):
        record = self.create()
        with self.assertRaises(ValidationError):
            self.service.act(A_MGR, record["id"], 1, 'delegate', {'phases': ['approve'], 'valid_until': hours(8)})
        detail = self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        self.assertEqual(detail["version"], 1)
        with self.assertRaises(Conflict):
            self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('丙分局', ['survey']))
        detail = self.service.act(B_MGR, record["id"], 1, 'return', {'reason': '船只冲突'})
        self.assertEqual(detail["version"], 1)
        detail = self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('丙分局', ['survey', 'splice']))
        self.assertEqual(detail["delegation"]["to_org"], "丙分局")
        statuses = [item["status"] for item in detail["delegations"]]
        self.assertEqual(statuses, ["active", "returned"])

    def test_concurrent_write_conflict_details(self):
        record = self.create()
        self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        winner = self.service.act(A_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-A'})
        self.assertEqual(winner["version"], 2)
        with self.assertRaises(Conflict) as ctx:
            self.service.act(B_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-B'})
        details = ctx.exception.details
        self.assertEqual(details["current_version"], 2)
        self.assertEqual(details["current_state"], "approved")
        self.assertEqual(details["jurisdiction_org"], "甲分局")
        self.assertEqual(details["entrusted_org"], "乙分局")
        self.assertEqual(details["conflicting_action"]["action"], "approve")
        self.assertEqual(details["conflicting_action"]["org"], "甲分局")
        record = self.service.get_record(ADMIN, record["id"])
        self.assertEqual(record["payload"]["repair_manager"], "RM-A")

    def test_delegation_guard_blocks_stale_write(self):
        record = self.create()
        detail = self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['approve']))
        delegation_id = detail["delegation"]["id"]
        self.service.act(ADMIN, record["id"], 1, 'revoke', {'reason': '上级统一调度'})
        with self.assertRaises(PermissionDenied):
            self.service.act(B_MGR, record["id"], 1, 'approve', {'repair_manager': 'RM-B'})
        with self.assertRaises(Conflict) as ctx:
            self.service.repository.mutate(
                record_id=record["id"],
                expected_version=1,
                state="approved",
                payload=record["payload"],
                actor_id="eng-b",
                action="approve",
                details={},
                expected_delegation_id=delegation_id,
            )
        self.assertIn("受托状态已变化", str(ctx.exception))

    def test_detail_shows_jurisdiction_delegation_handover(self):
        record = self.create()
        detail = self.service.act(A_MGR, record["id"], 1, 'delegate', delegation('乙分局', ['survey']))
        self.assertEqual(detail["jurisdiction_org"], "甲分局")
        self.assertEqual(detail["delegation"]["to_org"], "乙分局")
        self.assertEqual(detail["delegation"]["valid_until"] > detail["delegation"]["valid_from"], True)
        detail = self.service.act(B_ENG, record["id"], 1, 'return', {'reason': '海况超限'})
        self.assertIsNone(detail["delegation"])
        handover = detail["delegations"][0]
        self.assertEqual(handover["from_org"], "甲分局")
        self.assertEqual(handover["to_org"], "乙分局")
        self.assertEqual(handover["reason"], "海况超限")
        actions = [event["action"] for event in self.service.timeline(A_OP, record["id"])]
        self.assertEqual(actions, ["created", "delegate", "return"])

    def test_list_visibility_follows_delegation(self):
        first = self.create("S3")
        second = self.service.create(B_OP, "CABLE-S4", data("S4", "乙分局"))
        self.assertEqual([item["id"] for item in self.service.list_records(A_OP)], [first["id"]])
        self.assertEqual([item["id"] for item in self.service.list_records(B_OP)], [second["id"]])
        self.service.act(A_MGR, first["id"], 1, 'delegate', delegation('乙分局', ['survey']))
        visible = {item["id"] for item in self.service.list_records(B_OP)}
        self.assertEqual(visible, {first["id"], second["id"]})
        listed = [item for item in self.service.list_records(B_OP) if item["id"] == first["id"]][0]
        self.assertEqual(listed["delegation"]["to_org"], "乙分局")
        self.service.act(B_MGR, first["id"], 1, 'return', {'reason': '任务结束'})
        self.assertEqual([item["id"] for item in self.service.list_records(B_OP)], [second["id"]])


if __name__ == "__main__":
    unittest.main()
