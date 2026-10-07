"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, DomainError, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


DELEGATION_ACTIONS = {"delegate", "return", "revoke"}
TERMINAL_STATES = {"restored", "cancelled"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_visible(self, actor: Actor, record: Dict[str, Any], delegation: Optional[Dict[str, Any]]) -> None:
        if actor.role == "admin":
            return
        jurisdiction = record.get("jurisdiction_org") or ""
        if not jurisdiction:
            return
        if actor.organization and actor.organization == jurisdiction:
            return
        if delegation and actor.organization and actor.organization == delegation["to_org"]:
            return
        raise PermissionDenied("记录不在本分局管辖或受托范围内")

    def _ensure_write_scope(self, actor: Actor, record: Dict[str, Any], delegation: Optional[Dict[str, Any]], action: str) -> None:
        if actor.role == "admin":
            return
        jurisdiction = record.get("jurisdiction_org") or ""
        if not jurisdiction:
            return
        if actor.organization == jurisdiction:
            return
        if delegation and actor.organization == delegation["to_org"]:
            if action in delegation["phases"]:
                return
            raise PermissionDenied("受托阶段不包括%s" % action)
        raise PermissionDenied("该分局无权办理此记录")

    def _enrich(self, record: Dict[str, Any]) -> Dict[str, Any]:
        now = self._now()
        item = dict(record)
        item["delegation"] = self.repository.active_delegation(record["id"], now)
        item["delegations"] = self.repository.delegations_for(record["id"])
        return item

    def _conflict(self, record_id: int, exc: Conflict) -> Conflict:
        details: Dict[str, Any] = {}
        try:
            now = self._now()
            record = self.repository.get(record_id)
            delegation = self.repository.active_delegation(record_id, now)
            timeline = self.repository.audit_timeline(record_id)
            last = timeline[-1] if timeline else None
            details = {
                "record_id": record_id,
                "current_version": record["version"],
                "current_state": record["state"],
                "jurisdiction_org": record.get("jurisdiction_org") or "",
                "entrusted_org": delegation["to_org"] if delegation else "",
                "entrusted_phases": delegation["phases"] if delegation else [],
                "conflicting_action": {
                    "action": last["action"],
                    "actor_id": last["actor_id"],
                    "org": last["details"].get("org", ""),
                    "at": last["created_at"],
                } if last else None,
            }
        except DomainError:
            pass
        return Conflict(str(exc), details=details)

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        jurisdiction = prepared.get("jurisdiction_org") or actor.organization
        prepared["jurisdiction_org"] = jurisdiction
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, jurisdiction_org=jurisdiction)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now = self._now()
        self.repository.sweep_expired(now)
        if actor.role == "admin":
            records = self.repository.list_records(state=state, limit=limit)
        else:
            records = self.repository.list_visible_records(actor.organization, now, state=state, limit=limit)
        delegations = self.repository.active_delegations_map(now)
        return [dict(record, delegation=delegations.get(record["id"])) for record in records]

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now = self._now()
        self.repository.sweep_expired(now)
        record = self.repository.get(record_id)
        self._ensure_visible(actor, record, self.repository.active_delegation(record_id, now))
        return self._enrich(record)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if action in DELEGATION_ACTIONS:
            return self._act_delegation(actor, record_id, int(expected_version), action, data or {})
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        now = self._now()
        self.repository.sweep_expired(now)
        record = self.repository.get(record_id)
        delegation = self.repository.active_delegation(record_id, now)
        self._ensure_write_scope(actor, record, delegation, action)
        try:
            self.rules.require_transition(record, action)
            new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
            record = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state, "org": actor.organization},
                expected_delegation_id=delegation["id"] if delegation else None,
            )
        except Conflict as exc:
            raise self._conflict(record_id, exc)
        return self._enrich(record)

    def _act_delegation(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        now = self._now()
        self.repository.sweep_expired(now)
        record = self.repository.get(record_id)
        try:
            if action == "delegate":
                if not self.rules.role_can_delegate(actor.role):
                    raise PermissionDenied("仅抢修经理或上级可委托")
                if actor.role != "admin" and (not record["jurisdiction_org"] or actor.organization != record["jurisdiction_org"]):
                    raise PermissionDenied("仅管辖分局可委托兄弟分局")
                if record["state"] in TERMINAL_STATES:
                    raise Conflict("记录已结束，无法委托")
                spec = self.rules.validate_delegation(data, datetime.now(timezone.utc))
                if record["jurisdiction_org"] and spec["to_org"] == record["jurisdiction_org"]:
                    raise ValidationError("受托分局不能是管辖分局")
                from_org = record["jurisdiction_org"] or actor.organization
                self.repository.delegate(record_id, expected_version, from_org, spec, actor.user_id, actor.organization)
            else:
                if action == "revoke" and not self.rules.role_can_revoke(actor.role):
                    raise PermissionDenied("仅上级可撤回委托")
                delegation = self.repository.active_delegation(record_id, now)
                if delegation is None:
                    raise Conflict("当前没有生效中的委托")
                if action == "return":
                    if actor.role != "admin" and actor.organization != delegation["to_org"]:
                        raise PermissionDenied("仅受托分局可退回委托")
                    status = "returned"
                else:
                    status = "revoked"
                reason = text(data, "reason")
                self.repository.close_delegation(record_id, expected_version, status, actor.user_id, actor.organization, reason)
        except Conflict as exc:
            raise self._conflict(record_id, exc)
        return self._enrich(self.repository.get(record_id))

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now = self._now()
        self.repository.sweep_expired(now)
        record = self.repository.get(record_id)
        self._ensure_visible(actor, record, self.repository.active_delegation(record_id, now))
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
