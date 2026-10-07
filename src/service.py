"""业务用例编排、跨分局权限、委托生命周期与审计。"""
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, parse_iso, text, utc_now
from .repository import Repository
from .rules import DELEGATION_ENDED, STAGE_LABELS, DomainRules


# 交接事件（详情页展示管辖、受托与交接）
HANDOVER_ACTIONS = {
    "delegation_granted": "委托",
    "delegation_returned": "受托退回",
    "delegation_withdrawn": "上级撤回",
    "delegation_expired": "到期失效",
}
CONFLICT_ACTIONS = {"write_conflict"}
BUSINESS_ACTIONS = {"approve", "mobilize", "survey", "splice", "test", "restore", "cancel"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 clock: Callable[[], Any] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self._clock = clock

    def now(self):
        return self._clock() if self._clock else utc_now()

    def now_iso(self) -> str:
        return self.now().replace(microsecond=0).isoformat()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 读取与可见性 ----

    def _expire_due(self, record_id: Optional[int] = None) -> None:
        self.repository.expire_due(now_iso=self.now_iso(), record_id=record_id)

    def _effective(self, delegation: Optional[Dict[str, Any]]):
        if not delegation or delegation["status"] != "active":
            return None
        if parse_iso(delegation["valid_from"]) <= self.now() < parse_iso(delegation["valid_until"]):
            return delegation
        return None

    def _visible_record(self, actor: Actor, record_id: int) -> tuple:
        self._expire_due(record_id)
        record = self.repository.get(record_id)
        delegations = self.repository.list_delegations(record_id)
        delegation = next((d for d in reversed(delegations) if d["status"] == "active"), None)
        trustee = delegation["target_org"] if delegation else ""
        if not self.rules.can_view(actor, record["owner_org"], trustee):
            raise PermissionDenied("该故障单不属于本分局，无权查看")
        return record, delegation

    def _delegation_view(self, delegation: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not delegation:
            return None
        view = dict(delegation)
        view["stage_label"] = STAGE_LABELS.get(delegation["stage"], delegation["stage"])
        view["effective"] = self._effective(delegation) is not None
        return view

    def _conflict_events(self, record_id: int) -> List[Dict[str, Any]]:
        return [event for event in self.audit.timeline(record_id) if event["action"] in CONFLICT_ACTIONS]

    def _handover_events(self, record_id: int) -> List[Dict[str, Any]]:
        result = []
        for event in self.audit.timeline(record_id):
            if event["action"] in HANDOVER_ACTIONS:
                item = dict(event)
                item["label"] = HANDOVER_ACTIONS[event["action"]]
                result.append(item)
        return result

    def _detail_view(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        record, delegation = self._visible_record(actor, record_id)
        effective = self._effective(delegation)
        record["current_delegation"] = self._delegation_view(delegation)
        record["trustee_org"] = effective["target_org"] if effective else ""
        record["writer_org"] = self.rules.writer_for(record, effective)
        record["delegations"] = [self._delegation_view(d) for d in self.repository.list_delegations(record_id)]
        record["handovers"] = self._handover_events(record_id)
        record["conflicts"] = self._conflict_events(record_id)
        return record

    def _receipt_view(self, actor: Actor, record_id: int, audit_action: str) -> Dict[str, Any]:
        """交接动作回执：只返回本次交接凭据，不重新开放单据可见性。"""
        record = self.repository.get(record_id)
        event = next((e for e in reversed(self.audit.timeline(record_id))
                      if e["action"] == audit_action and e["actor_id"] == actor.user_id), None)
        return {"id": record_id, "reference": record["reference"], "version": record["version"],
                "state": record["state"], "owner_org": record["owner_org"], "trustee_org": "",
                "writer_org": record["owner_org"], "receipt": event}

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        payload = payload or {}
        requested_org = str(payload.get("owner_org") or "").strip()
        if self.rules.is_superior(actor):
            owner_org = requested_org or actor.organization.strip()
            if not owner_org:
                raise PermissionDenied("上级代建单须写明管辖分局")
        else:
            owner_org = self.rules.require_org(actor)
            if requested_org and requested_org != owner_org:
                raise PermissionDenied("只能登记本分局管辖的故障单")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload)
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, owner_org)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.is_superior(actor):
            self.rules.require_org(actor)
        self._expire_due()
        records = self.repository.list_records(state=state, limit=limit)
        active_map = self.repository.active_delegation_map([record["id"] for record in records])
        items: List[Dict[str, Any]] = []
        for record in records:
            delegation = self._effective(active_map.get(record["id"]))
            trustee = delegation["target_org"] if delegation else ""
            if not self.rules.can_view(actor, record["owner_org"], trustee):
                continue
            record["trustee_org"] = trustee
            record["writer_org"] = self.rules.writer_for(record, delegation)
            record["delegation_stage"] = delegation["stage"] if delegation else ""
            items.append(record)
        return items

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._detail_view(actor, record_id)

    # ---- 业务动作（单分局写入） ----

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        now_iso = self.now_iso()
        try:
            with self.repository.tx() as connection:
                self.repository.expire_due(connection, now_iso, record_id)
                record = self.repository.get_conn(connection, record_id)
                delegation = self.repository.active_delegation_conn(connection, record_id)
                effective = self._effective(delegation)
                self.rules.authorize_write(actor, record, action, effective)
                new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
                version = self.repository.update_state_conn(
                    connection, record_id, int(expected_version), new_state, new_payload, actor.user_id, now_iso
                )
                self.repository.add_audit_conn(
                    connection, record_id, actor.user_id, actor.organization.strip(), action, version,
                    {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state,
                     "writer_org": self.rules.writer_for(record, effective)},
                    now_iso,
                )
                result = self.repository.get_conn(connection, record_id)
        except Conflict as conflict:
            if "expected_version" not in conflict.details:
                conflict.details["expected_version"] = int(expected_version)
            self._note_write_conflict(actor, record_id, action, data, conflict, now_iso)
            raise
        return result

    def _note_write_conflict(self, actor: Actor, record_id: int, action: str,
                             data: Dict[str, Any], conflict: Conflict, now_iso: str) -> None:
        """换手失败也要留下凭据：保留原记录，记录受托方与冲突动作。"""
        try:
            self._expire_due(record_id)
            record = self.repository.get(record_id)
            delegation = self.repository.list_delegations(record_id)
            active = next((d for d in reversed(delegation) if d["status"] == "active"), None)
            effective = self._effective(active)
            trustee = effective["target_org"] if effective else ""
            if not self.rules.can_view(actor, record["owner_org"], trustee):
                return
            timeline = self.audit.timeline(record_id)
            winner = next((e for e in reversed(timeline) if e["action"] in BUSINESS_ACTIONS
                          or e["action"] in HANDOVER_ACTIONS), None)
            details = dict(conflict.details)
            details.update({
                "attempted_action": action,
                "input": data or {},
                "expected_version": conflict.details.get("expected_version"),
                "current_version": record["version"],
                "writer_org": self.rules.writer_for(record, effective),
                "trustee_org": trustee,
                "winning_action": winner["action"] if winner else "",
                "winning_actor": winner["actor_id"] if winner else "",
            })
            self.repository.add_audit(record_id, actor.user_id, actor.organization.strip(),
                                      "write_conflict", details)
            conflict.details = details
        except Exception:
            return

    # ---- 委托：发起 / 退回 / 撤回 ----

    def grant_delegation(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now_iso = self.now_iso()
        try:
            with self.repository.tx() as connection:
                self.repository.expire_due(connection, now_iso, record_id)
                record = self.repository.get_conn(connection, record_id)
                existing = self.repository.active_delegation_conn(connection, record_id)
                if existing is not None:
                    raise Conflict(
                        "已有在效委托（受托分局：%s），请先退回或撤回" % existing["target_org"],
                        {"reason": "delegation_active", "current_version": record["version"],
                         "delegation_id": existing["id"], "writer_org": existing["target_org"],
                         "conflict_action": "delegation_granted"},
                    )
                fields = self.rules.validate_delegation(record, data or {}, actor, now_iso)
                version = self.repository.bump_version_conn(
                    connection, record_id, int(expected_version), actor.user_id, now_iso
                )
                delegation = self.repository.grant_delegation_conn(
                    connection, record_id, fields, actor.user_id, actor.organization.strip(), now_iso
                )
                self.repository.add_audit_conn(
                    connection, record_id, actor.user_id, actor.organization.strip(),
                    "delegation_granted", version,
                    {"delegation_id": delegation["id"], "target_org": fields["target_org"],
                     "stage": fields["stage"], "stage_label": STAGE_LABELS.get(fields["stage"], fields["stage"]),
                     "valid_from": fields["valid_from"], "valid_until": fields["valid_until"],
                     "reason": fields["reason"]},
                    now_iso,
                )
        except Conflict as conflict:
            if "expected_version" not in conflict.details:
                conflict.details["expected_version"] = int(expected_version)
            self._note_write_conflict(actor, record_id, "delegation_granted", data, conflict, now_iso)
            raise
        return self._detail_view(actor, record_id)

    def return_delegation(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._end_delegation(actor, record_id, expected_version, data, "returned")

    def withdraw_delegation(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._end_delegation(actor, record_id, expected_version, data, "withdrawn")
    def _end_delegation(self, actor: Actor, record_id: int, expected_version: int,
                        data: Dict[str, Any], end_status: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        reason = text({"reason": (data or {}).get("reason", "")}, "reason")
        now_iso = self.now_iso()
        audit_action = "delegation_returned" if end_status == "returned" else "delegation_withdrawn"
        try:
            with self.repository.tx() as connection:
                self.repository.expire_due(connection, now_iso, record_id)
                record = self.repository.get_conn(connection, record_id)
                delegation = self.repository.active_delegation_conn(connection, record_id)
                if delegation is None:
                    raise Conflict("没有在效委托，无需%s" % ("退回" if end_status == "returned" else "撤回"))
                if end_status == "returned":
                    org = self.rules.require_org(actor)
                    if self.rules.is_superior(actor):
                        raise PermissionDenied("上级请使用撤回")
                    if org != delegation["target_org"]:
                        raise PermissionDenied("仅受托分局%s可退回" % delegation["target_org"])
                else:
                    if not self.rules.is_superior(actor):
                        raise PermissionDenied("仅上级可撤回委托")
                ended = self.repository.end_delegation_conn(
                    connection, delegation["id"], end_status, actor.user_id,
                    actor.organization.strip(), now_iso, reason
                )
                version = self.repository.bump_version_conn(
                    connection, record_id, int(expected_version), actor.user_id, now_iso
                )
                self.repository.add_audit_conn(
                    connection, record_id, actor.user_id, actor.organization.strip(), audit_action, version,
                    {"delegation_id": ended["id"], "target_org": ended["target_org"], "stage": ended["stage"],
                     "reason": reason},
                    now_iso,
                )
        except Conflict as conflict:
            if "expected_version" not in conflict.details:
                conflict.details["expected_version"] = int(expected_version)
            if not conflict.details.get("persisted"):
                self._note_write_conflict(actor, record_id, audit_action, data or {}, conflict, now_iso)
            raise
        if self.rules.is_superior(actor):
            return self._detail_view(actor, record_id)
        # 受托方退回后立即失去可见性，仅返回交接回执
        return self._receipt_view(actor, record_id, audit_action)

    def stages(self, actor: Actor) -> List[Dict[str, str]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.rules.stages()

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._visible_record(actor, record_id)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.is_superior(actor):
            self.rules.require_org(actor)
        self._expire_due()
        result: Dict[str, int] = {}
        for record in self.repository.list_records(limit=500):
            active_map = self.repository.active_delegation_map([record["id"]])
            effective = self._effective(active_map.get(record["id"]))
            trustee = effective["target_org"] if effective else ""
            if self.rules.can_view(actor, record["owner_org"], trustee):
                result[record["state"]] = result.get(record["state"], 0) + 1
        return result
