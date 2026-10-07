"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError, boolean, choice, integer, iso_time, number, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator', 'repair_manager'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}

# 抢修阶段到业务动作的映射；all 表示整段流程（抢修经理按阶段委托兄弟分局）。
STAGE_ACTIONS: Dict[str, set] = {
    'approve': {'approve'},
    'mobilize': {'mobilize'},
    'survey': {'survey'},
    'splice': {'splice'},
    'test': {'test'},
    'restore': {'restore', 'cancel'},
    'all': set(TRANSITIONS.keys()),
}
STAGE_LABELS: Dict[str, str] = {
    'approve': '方案批准',
    'mobilize': '船机动员',
    'survey': '故障勘察',
    'splice': '光缆接续',
    'test': '系统测试',
    'restore': '恢复收口',
    'all': '全流程',
}
# 委托终止状态
DELEGATION_ENDED = {'returned', 'withdrawn', 'expired'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def is_superior(self, actor: Actor) -> bool:
        """上级（调度中心）可跨分局查看并撤回委托。"""
        return actor.role == "admin"

    def require_org(self, actor: Actor) -> str:
        if not actor.organization.strip():
            raise PermissionDenied("缺少所属分局（X-Org）")
        return actor.organization.strip()

    def can_view(self, actor: Actor, owner_org: str, trustee_org: str) -> bool:
        """本分局和当前受托分局可见；上级可见全部。"""
        org = actor.organization.strip()
        if self.is_superior(actor):
            return True
        if not org:
            return False
        return org == owner_org or bool(trustee_org and org == trustee_org)

    def writer_for(self, record: Dict[str, Any], delegation: Dict[str, Any] = None) -> str:
        """当前唯一允许写入的分局：委托生效期内为受托分局，否则为管辖分局。"""
        if delegation and delegation["status"] == "active":
            return str(delegation["target_org"])
        return str(record["owner_org"])

    def authorize_write(self, actor: Actor, record: Dict[str, Any], action: str, delegation: Dict[str, Any] = None) -> None:
        """并发办理只让一个分局写入，按委托阶段约束受托方动作范围。"""
        org = self.require_org(actor)
        if not self.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if delegation and delegation["status"] == "active":
            stage = str(delegation["stage"])
            if org == delegation["target_org"]:
                if action not in STAGE_ACTIONS.get(stage, set()):
                    raise PermissionDenied("委托阶段[%s]不含动作%s" % (STAGE_LABELS.get(stage, stage), action))
                return
            if org == record["owner_org"] and action in STAGE_ACTIONS.get(stage, set()):
                # 该阶段已委托给兄弟分局：保留原记录，告知当前受托方与冲突动作
                raise Conflict(
                    "该阶段已委托给%s办理" % delegation["target_org"],
                    {"reason": "stage_delegated", "writer_org": delegation["target_org"],
                     "delegated_stage": stage, "conflict_action": action,
                     "delegation_id": delegation["id"]},
                )
            raise PermissionDenied("当前仅受托分局%s可写入" % delegation["target_org"])
        if org != record["owner_org"]:
            raise PermissionDenied("故障单归属%s分局，本分局无权办理" % record["owner_org"])

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 跨分局委托规则 ----

    def stages(self) -> List[Dict[str, str]]:
        return [{"stage": stage, "label": STAGE_LABELS[stage]} for stage in STAGE_ACTIONS if stage != "all"] + [
            {"stage": "all", "label": STAGE_LABELS["all"]}
        ]

    def validate_delegation(self, record: Dict[str, Any], data: Dict[str, Any], actor: Actor, now_iso: str) -> Dict[str, Any]:
        """抢修经理按阶段委托兄弟分局并设生效期。"""
        owner = str(record["owner_org"])
        org = self.require_org(actor)
        if not self.is_superior(actor):
            if actor.role != "repair_manager":
                raise PermissionDenied("仅抢修经理可发起委托")
            if org != owner:
                raise PermissionDenied("仅管辖分局可委托本单")
        target = text(data, "target_org")
        if target == owner:
            raise PermissionDenied("受托分局必须是兄弟分局，不能与管辖分局相同")
        stage = choice(data, "stage", list(STAGE_ACTIONS.keys()))
        valid_from = iso_time(data, "valid_from") if data.get("valid_from") else now_iso
        valid_until = iso_time(data, "valid_until")
        if valid_until <= valid_from:
            raise ValidationError("生效截止必须晚于生效时间")
        reason = text(data, "reason")
        return {"target_org": target, "stage": stage, "valid_from": valid_from,
                "valid_until": valid_until, "reason": reason}

    def delegation_effective(self, delegation: Dict[str, Any], now_dt) -> bool:
        from .domain import parse_iso
        if delegation["status"] != "active":
            return False
        return parse_iso(delegation["valid_from"]) <= now_dt < parse_iso(delegation["valid_until"])
