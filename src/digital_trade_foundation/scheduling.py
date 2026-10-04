"""会谈编排核心领域服务。

在基础登记能力之上实现：

* 以代表团与机构关联为基础的参会资格核验；
* 议题敏感级别、跨时区时间窗、译员语言能力、场地容量、回避关系与互斥场次约束；
* 邀请的候补、接受、转授权、退出、改期生命周期与有限期限保留；
* 截止后按稳定次序自动递补，恢复后沿用原候补位置与到期时间；
* 材料权限跟随有效席位与承诺版本开放或收回；
* 会谈结束、签到与访问等既成事实不可被新名单覆盖。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Authorization,
    Delegation,
    Eligibility,
    Language,
    MaterialGrant,
    MyScheduleItem,
    OccurrenceFact,
    Representative,
    Resource,
    ResourceScheduleItem,
    Seat,
    SeatHistoryItem,
    Session,
    SessionChange,
    SessionView,
)
from .storage import Database

# 敏感级别由低到高；数值可比较，便于资格判断。
SENSITIVITY_LEVELS = {"open": 10, "controlled": 20, "confidential": 30, "secret": 40}
SESSION_TYPES = frozenset({"minister_closed", "procurement", "technical_dd", "plenary", "bilateral"})
RESOURCE_KINDS = frozenset({"interpreter", "room", "observer"})
LANGUAGE_LEVELS = frozenset({"working", "fluent", "native"})

# 当前占用席位的承诺状态（持有与确认）。
EFFECTIVE_SEAT_STATES = frozenset({"invited", "accepted"})
# 终态：席位已释放，不再参与容量与资源计算。
TERMINAL_SEAT_STATES = frozenset({"declined", "withdrawn", "cancelled", "delegated"})
ACTIVE_SESSION_STATUSES = frozenset({"scheduled", "confirmed"})

DEFAULT_HOLD_MINUTES = 24 * 60


def parse_dt(value: str) -> datetime:
    """把 UTC ISO 文本解析为带时区时间。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("时间格式无效，应为 ISO 8601 UTC 文本") from exc
    if parsed.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def fmt_dt(value: datetime) -> str:
    """格式化为服务统一使用的 UTC 文本。"""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """判断两个半开时间窗是否重叠。"""

    return start_a < end_b and start_b < end_a


class SchedulingService:
    """实现会谈编排的全部事务性用例。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 hold_minutes: int = DEFAULT_HOLD_MINUTES) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.hold_minutes = hold_minutes

    # ------------------------------------------------------------------ 工具

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_text(self) -> str:
        return fmt_dt(self._now())

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 64 or not all(c.isalnum() or c in "_-." for c in value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _sensitivity(self, value: str) -> str:
        value = str(value or "").strip()
        if value not in SENSITIVITY_LEVELS:
            raise ValidationError("sensitivity 必须是 open/controlled/confidential/secret")
        return value

    def _actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return dict(row)

    def _require_roles(self, actor: dict[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        resource_type, resource_id, response = create()
        response = dict(response)
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )
        response["replayed"] = False
        return response

    def verify_audit(self) -> tuple[bool, int]:
        """校验哈希审计链的完整性与事件数。"""

        with self.database.reading() as connection:
            return verify_chain(connection)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_text())

    # ------------------------------------------------------------ 登记：代表团

    def register_delegation(self, *, request_id: str, actor_id: str, delegation_id: str,
                            organization_id: str, name: str, liaison_actor_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "delegation_id": delegation_id,
                   "organization_id": organization_id, "name": name, "liaison_actor_id": liaison_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            delegation_id = self._id(delegation_id, "delegation_id")
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("机构不存在")
            if liaison_actor_id is not None:
                liaison = self._actor(connection, liaison_actor_id)
                if liaison["organization_id"] != organization_id and actor["role"] != "admin":
                    raise PermissionDenied("联络人必须隶属于代表团主办机构")
            else:
                liaison_actor_id = actor_id

            def create():
                try:
                    connection.execute(
                        "INSERT INTO delegations(delegation_id,organization_id,name,liaison_actor_id,active,created_at)"
                        " VALUES(?,?,?,?,1,?)",
                        (delegation_id, organization_id, name, liaison_actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("代表团编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="delegation.registered",
                            resource_type="delegation", resource_id=delegation_id,
                            detail={"organization_id": organization_id, "name": name,
                                    "liaison_actor_id": liaison_actor_id})
                return "delegation", delegation_id, {"delegation_id": delegation_id}

            return self._idempotent(connection, request_id=request_id, action="register_delegation",
                                    payload=payload, create=create)

    def _delegation_row(self, connection, delegation_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        if row is None:
            raise NotFoundError("代表团不存在")
        return dict(row)

    def get_delegation(self, delegation_id: str) -> Delegation:
        with self.database.reading() as connection:
            row = connection.execute("SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
            if row is None:
                raise NotFoundError("代表团不存在")
            return Delegation(row["delegation_id"], row["organization_id"], row["name"],
                              row["liaison_actor_id"], bool(row["active"]))

    # ------------------------------------------------------------ 登记：代表

    def register_representative(self, *, request_id: str, actor_id: str, representative_id: str,
                                delegation_id: str, organization_id: str, display_name: str,
                                clearance: str = "open") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "representative_id": representative_id,
                   "delegation_id": delegation_id, "organization_id": organization_id,
                   "display_name": display_name, "clearance": clearance}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            delegation = self._delegation_row(connection, delegation_id)
            if actor["role"] not in ("admin", "operator") and actor["actor_id"] != delegation["liaison_actor_id"]:
                raise PermissionDenied("只有主办操作员或代表团联络人可以登记代表")
            if actor["actor_id"] == delegation["liaison_actor_id"] and organization_id != actor["organization_id"] \
                    and actor["role"] not in ("admin", "operator"):
                raise PermissionDenied("联络人只能登记本机构关联代表")
            representative_id = self._id(representative_id, "representative_id")
            display_name = self._text(display_name, "display_name")
            clearance = self._sensitivity(clearance)
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("代表所属机构不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO representatives(representative_id,delegation_id,organization_id,display_name,"
                        "clearance,active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (representative_id, delegation_id, organization_id, display_name,
                         clearance, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("代表编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="representative.registered",
                            resource_type="representative", resource_id=representative_id,
                            detail={"delegation_id": delegation_id, "organization_id": organization_id,
                                    "display_name": display_name, "clearance": clearance})
                return "representative", representative_id, {"representative_id": representative_id}

            return self._idempotent(connection, request_id=request_id, action="register_representative",
                                    payload=payload, create=create)

    def _rep_row(self, connection, representative_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM representatives WHERE representative_id=?",
                                 (representative_id,)).fetchone()
        if row is None:
            raise NotFoundError("代表不存在")
        return dict(row)

    def get_representative(self, representative_id: str) -> Representative:
        with self.database.reading() as connection:
            row = self._rep_row(connection, representative_id)
        return Representative(row["representative_id"], row["delegation_id"], row["organization_id"],
                              row["display_name"], bool(row["active"]))

    def update_clearance(self, *, request_id: str, actor_id: str, representative_id: str,
                         clearance: str) -> dict[str, Any]:
        """合规调整代表的知悉级别；立即影响后续资格与下载判断。"""

        payload = {"actor_id": actor_id, "representative_id": representative_id, "clearance": clearance}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "reviewer")
            rep = self._rep_row(connection, representative_id)
            clearance = self._sensitivity(clearance)

            def create():
                connection.execute("UPDATE representatives SET clearance=? WHERE representative_id=?",
                                   (clearance, representative_id))
                self._audit(connection, actor_id=actor_id, action="representative.clearance_changed",
                            resource_type="representative", resource_id=representative_id,
                            detail={"from": rep["clearance"], "to": clearance})
                return "representative", representative_id, {"representative_id": representative_id,
                                                              "clearance": clearance}

            return self._idempotent(connection, request_id=request_id, action="update_clearance",
                                    payload=payload, create=create)

    def check_eligibility(self, connection, rep: dict[str, Any], session: dict[str, Any]) -> Eligibility:
        """以代表团与机构关联为基础核验参会资格，给出全部不满足原因。"""

        reasons: list[str] = []
        delegation = connection.execute("SELECT * FROM delegations WHERE delegation_id=?",
                                        (rep["delegation_id"],)).fetchone()
        if delegation is None or not delegation["active"]:
            reasons.append("delegation_inactive")
        if not rep["active"]:
            reasons.append("representative_inactive")
        if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                              (rep["organization_id"],)).fetchone() is None:
            reasons.append("organization_unlinked")
        if SENSITIVITY_LEVELS[rep["clearance"]] < SENSITIVITY_LEVELS[session["sensitivity"]]:
            reasons.append("clearance_insufficient")
        return Eligibility(rep["representative_id"], not reasons, tuple(reasons))

    # ------------------------------------------------------------ 登记：资源

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str, kind: str,
                          label: str, capacity: int = 1, sensitivity: str = "open",
                          site_id: str | None = None, languages: list[dict[str, str]] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "resource_id": resource_id, "kind": kind, "label": label,
                   "capacity": capacity, "sensitivity": sensitivity, "site_id": site_id,
                   "languages": languages or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "resource_manager")
            resource_id = self._id(resource_id, "resource_id")
            label = self._text(label, "label")
            if kind not in RESOURCE_KINDS:
                raise ValidationError("kind 必须是 interpreter/room/observer")
            if not isinstance(capacity, int) or capacity < 1:
                raise ValidationError("capacity 必须是不小于 1 的整数")
            sensitivity = self._sensitivity(sensitivity)
            if site_id is not None and connection.execute("SELECT 1 FROM sites WHERE site_id=?",
                                                          (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            normalized_languages: list[dict[str, str]] = []
            if kind == "interpreter":
                if not languages:
                    raise ValidationError("译员必须登记至少一种工作语言")
                seen: set[str] = set()
                for item in languages:
                    code = self._text(item.get("language_code", ""), "language_code", 16)
                    level = str(item.get("level", "working")).strip()
                    if level not in LANGUAGE_LEVELS:
                        raise ValidationError("语言级别必须是 working/fluent/native")
                    if code in seen:
                        raise ValidationError(f"语言 {code} 重复登记")
                    seen.add(code)
                    normalized_languages.append({"language_code": code, "level": level})

            def create():
                try:
                    connection.execute(
                        "INSERT INTO resources(resource_id,organization_id,kind,label,capacity,sensitivity,"
                        "site_id,languages_json,active,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                        (resource_id, actor["organization_id"], kind, label, capacity, sensitivity,
                         site_id, canonical_json(normalized_languages), self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="resource.registered",
                            resource_type="resource", resource_id=resource_id,
                            detail={"kind": kind, "label": label, "capacity": capacity,
                                    "sensitivity": sensitivity, "languages": normalized_languages})
                return "resource", resource_id, {"resource_id": resource_id}

            return self._idempotent(connection, request_id=request_id, action="register_resource",
                                    payload=payload, create=create)

    def _resource_row(self, connection, resource_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def get_resource(self, resource_id: str) -> Resource:
        with self.database.reading() as connection:
            row = self._resource_row(connection, resource_id)
        languages = tuple(Language(item["language_code"], item["level"])
                          for item in json.loads(row["languages_json"]))
        return Resource(row["resource_id"], row["kind"], row["label"], row["capacity"],
                        row["site_id"], languages, bool(row["active"]))

    def add_resource_block(self, *, request_id: str, actor_id: str, resource_id: str,
                           starts_at: str, ends_at: str, reason: str) -> dict[str, Any]:
        """登记资源不可用时间窗（译员请假、会议室维护等）。"""

        payload = {"actor_id": actor_id, "resource_id": resource_id, "starts_at": starts_at,
                   "ends_at": ends_at, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            resource = self._resource_row(connection, resource_id)
            if actor["role"] not in ("admin", "operator", "resource_manager"):
                raise PermissionDenied("当前角色不能登记资源封闭时间")
            if actor["role"] == "resource_manager" and resource["organization_id"] != actor["organization_id"]:
                raise PermissionDenied("不能封闭其他机构的资源")
            start = parse_dt(starts_at)
            end = parse_dt(ends_at)
            if end <= start:
                raise ValidationError("结束时间必须晚于开始时间")
            reason = self._text(reason, "reason")
            block_id = uuid.uuid4().hex

            def create():
                connection.execute(
                    "INSERT INTO resource_blocks(block_id,resource_id,starts_at,ends_at,reason,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (block_id, resource_id, start.isoformat().replace("+00:00", "Z"),
                     end.isoformat().replace("+00:00", "Z"), reason, actor_id, self._now_text()),
                )
                self._audit(connection, actor_id=actor_id, action="resource_block.added",
                            resource_type="resource", resource_id=resource_id,
                            detail={"block_id": block_id, "starts_at": starts_at, "ends_at": ends_at,
                                    "reason": reason})
                return "resource_block", block_id, {"block_id": block_id}

            return self._idempotent(connection, request_id=request_id, action="add_resource_block",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 登记：回避

    def add_recusal(self, *, request_id: str, actor_id: str, representative_id: str,
                    scope_type: str, scope_value: str, reason: str) -> dict[str, Any]:
        """登记回避关系：代表须回避另一代表或某机构的全部代表。"""

        payload = {"actor_id": actor_id, "representative_id": representative_id,
                   "scope_type": scope_type, "scope_value": scope_value, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "reviewer")
            rep = self._rep_row(connection, representative_id)
            if scope_type not in ("representative", "organization"):
                raise ValidationError("scope_type 必须是 representative 或 organization")
            scope_value = self._id(scope_value, "scope_value")
            if scope_type == "representative":
                self._rep_row(connection, scope_value)
            elif connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                    (scope_value,)).fetchone() is None:
                raise NotFoundError("被回避机构不存在")
            reason = self._text(reason, "reason")
            recusal_id = uuid.uuid4().hex

            def create():
                try:
                    connection.execute(
                        "INSERT INTO recusals(recusal_id,representative_id,scope_type,scope_value,reason,active,"
                        "created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
                        (recusal_id, representative_id, scope_type, scope_value, reason,
                         actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("回避关系已经登记") from exc
                self._audit(connection, actor_id=actor_id, action="recusal.added",
                            resource_type="recusal", resource_id=recusal_id,
                            detail={"representative_id": rep["representative_id"], "scope_type": scope_type,
                                    "scope_value": scope_value, "reason": reason})
                return "recusal", recusal_id, {"recusal_id": recusal_id}

            return self._idempotent(connection, request_id=request_id, action="add_recusal",
                                    payload=payload, create=create)

    def _recusal_conflict(self, connection, candidate_id: str, participant_ids: list[str]) -> dict[str, Any] | None:
        """候选代表与在场有效代表之间任意方向的回避冲突。"""

        if not participant_ids:
            return None
        participants = connection.execute(
            f"SELECT representative_id, organization_id FROM representatives WHERE representative_id IN "
            f"({','.join('?' * len(participant_ids))})",
            tuple(participant_ids),
        ).fetchall()
        org_by_id = {row["representative_id"]: row["organization_id"] for row in participants}
        candidate = self._rep_row(connection, candidate_id)
        # 候选主动回避在场代表或其机构。
        forward = connection.execute(
            "SELECT scope_type, scope_value FROM recusals WHERE active=1 AND representative_id=?",
            (candidate_id,),
        ).fetchall()
        for row in forward:
            if row["scope_type"] == "representative" and row["scope_value"] in org_by_id:
                return {"code": "recusal", "representative_id": candidate_id,
                        "other_representative_id": row["scope_value"], "direction": "candidate_avoids"}
            if row["scope_type"] == "organization" and row["scope_value"] in org_by_id.values():
                other = next(rid for rid, org in org_by_id.items() if org == row["scope_value"])
                return {"code": "recusal", "representative_id": candidate_id,
                        "other_representative_id": other, "direction": "candidate_avoids_organization"}
        # 在场代表回避候选或候选所在机构。
        backward = connection.execute(
            "SELECT representative_id, scope_type, scope_value FROM recusals WHERE active=1 AND representative_id IN "
            f"({','.join('?' * len(participant_ids))})",
            tuple(participant_ids),
        ).fetchall()
        for row in backward:
            if (row["scope_type"] == "representative" and row["scope_value"] == candidate_id) or \
               (row["scope_type"] == "organization" and row["scope_value"] == candidate["organization_id"]):
                return {"code": "recusal", "representative_id": row["representative_id"],
                        "other_representative_id": candidate_id, "direction": "participant_avoids_candidate"}
        return None

    # ------------------------------------------------------------ 登记：场次

    def create_session(self, *, request_id: str, actor_id: str, session_id: str, title: str,
                       session_type: str, sensitivity: str, starts_at: str, ends_at: str,
                       timezone_name: str, venue_id: str, capacity: int,
                       language_codes: list[str] | None = None,
                       exclusive_group: str | None = None,
                       resource_ids: list[str] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "session_id": session_id, "title": title,
                   "session_type": session_type, "sensitivity": sensitivity, "starts_at": starts_at,
                   "ends_at": ends_at, "timezone_name": timezone_name, "venue_id": venue_id,
                   "capacity": capacity, "language_codes": language_codes or [],
                   "exclusive_group": exclusive_group, "resource_ids": resource_ids or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            session_id = self._id(session_id, "session_id")
            title = self._text(title, "title")
            if session_type not in SESSION_TYPES:
                raise ValidationError("session_type 不在允许范围内")
            sensitivity = self._sensitivity(sensitivity)
            start = parse_dt(starts_at)
            end = parse_dt(ends_at)
            if end <= start:
                raise ValidationError("结束时间必须晚于开始时间")
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                raise ValidationError("timezone_name 不是有效 IANA 时区") from None
            if not isinstance(capacity, int) or capacity < 1:
                raise ValidationError("capacity 必须是不小于 1 的整数")
            venue = self._resource_row(connection, venue_id)
            if venue["kind"] != "room":
                raise ValidationError("venue_id 必须是会议室资源")
            if venue["capacity"] < capacity:
                raise ValidationError("会议室容量小于场次容量")
            if SENSITIVITY_LEVELS[venue["sensitivity"]] < SENSITIVITY_LEVELS[sensitivity]:
                raise ValidationError("会议室保密等级不足以承接该敏感级别场次")
            languages: list[str] = []
            for code in language_codes or []:
                code = self._text(code, "language_code", 16)
                if code not in languages:
                    languages.append(code)
            if exclusive_group is not None:
                exclusive_group = self._id(exclusive_group, "exclusive_group")
            attached: list[str] = []
            for resource_id in resource_ids or []:
                row = self._resource_row(connection, resource_id)
                if SENSITIVITY_LEVELS[row["sensitivity"]] < SENSITIVITY_LEVELS[sensitivity]:
                    raise ValidationError(f"资源 {resource_id} 保密等级不足")
                if resource_id not in attached and resource_id != venue_id:
                    attached.append(resource_id)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO sessions(session_id,title,session_type,sensitivity,starts_at,ends_at,"
                        "timezone_name,venue_id,capacity,languages_json,exclusive_group,status,version,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'scheduled',1,?,?)",
                        (session_id, title, session_type, sensitivity, fmt_dt(start), fmt_dt(end),
                         timezone_name, venue_id, capacity, canonical_json(languages), exclusive_group,
                         actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("场次编号已经存在") from exc
                connection.execute(
                    "INSERT INTO session_versions(session_id,version,fields_json,changed_by,changed_at)"
                    " VALUES(?,1,?,?,?)",
                    (session_id, canonical_json(payload), actor_id, self._now_text()),
                )
                all_resources = attached + [venue_id]
                for resource_id in dict.fromkeys(all_resources):
                    connection.execute(
                        "INSERT OR IGNORE INTO session_resources(session_id,resource_id,created_at) VALUES(?,?,?)",
                        (session_id, resource_id, self._now_text()),
                    )
                # 建场即预检：封闭时间窗、译员语言能力与资源冲突随事务回滚。
                created = self._session_row(connection, session_id)
                resource_conflicts = self._resource_conflicts(connection, created)
                if resource_conflicts:
                    raise ConflictError("场次资源保障存在冲突：" + canonical_json(resource_conflicts))
                self._audit(connection, actor_id=actor_id, action="session.created",
                            resource_type="session", resource_id=session_id,
                            detail={"title": title, "session_type": session_type,
                                    "sensitivity": sensitivity, "starts_at": fmt_dt(start),
                                    "ends_at": fmt_dt(end), "venue_id": venue_id, "capacity": capacity,
                                    "languages": languages, "exclusive_group": exclusive_group,
                                    "resource_ids": attached})
                return "session", session_id, {"session_id": session_id, "version": 1}

            return self._idempotent(connection, request_id=request_id, action="create_session",
                                    payload=payload, create=create)

    def _session_row(self, connection, session_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("场次不存在")
        return dict(row)

    def _session_model(self, row: dict[str, Any], connection=None) -> Session:
        connection = connection or self.database.connection
        commitment_version = connection.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM seat_versions WHERE session_id=?",
            (row["session_id"],)).fetchone()["v"]
        return Session(
            row["session_id"], row["title"], row["session_type"], row["sensitivity"],
            row["starts_at"], row["ends_at"], row["timezone_name"], row["venue_id"],
            row["capacity"], tuple(json.loads(row["languages_json"])),
            row["exclusive_group"], row["status"], row["version"], commitment_version,
        )

    def get_session(self, session_id: str) -> Session:
        with self.database.reading() as connection:
            return self._session_model(self._session_row(connection, session_id))

    def add_exclusion(self, *, request_id: str, actor_id: str, session_id_a: str,
                      session_id_b: str) -> dict[str, Any]:
        """登记两个场次互斥（同一名代表不能同时进入名单）。"""

        payload = {"actor_id": actor_id, "session_id_a": session_id_a, "session_id_b": session_id_b}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            a, b = sorted((self._id(session_id_a, "session_id_a"), self._id(session_id_b, "session_id_b")))
            if a == b:
                raise ValidationError("场次不能与自身互斥")
            self._session_row(connection, a)
            self._session_row(connection, b)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO session_exclusions(session_id_a,session_id_b,created_at) VALUES(?,?,?)",
                        (a, b, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("互斥关系已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="session_exclusion.added",
                            resource_type="session", resource_id=a,
                            detail={"session_id_a": a, "session_id_b": b})
                return "session_exclusion", f"{a}|{b}", {"session_id_a": a, "session_id_b": b}

            return self._idempotent(connection, request_id=request_id, action="add_exclusion",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 冲突检测

    def _booked_sessions(self, connection, resource_id: str, start: datetime, end: datetime,
                         exclude_session_id: str | None = None):
        """返回时间窗重叠且仍在保留/确认中的其他场次。

        会议室与译员在场次排定时即被保留（有限期保留以场次开始时间为界），
        场次取消或结束后才释放；因此不要求已有有效席位。
        """

        rows = connection.execute(
            "SELECT s.* FROM sessions s JOIN session_resources sr ON sr.session_id=s.session_id "
            "WHERE sr.resource_id=? AND s.status IN ('scheduled','confirmed')",
            (resource_id,),
        ).fetchall()
        return [dict(row) for row in rows
                if not (exclude_session_id and row["session_id"] == exclude_session_id)
                and overlaps(start, end, parse_dt(row["starts_at"]), parse_dt(row["ends_at"]))]

    def _resource_conflicts(self, connection, session: dict[str, Any]) -> list[dict[str, Any]]:
        """检测场地与保障资源在时间窗内的可用性及译员语言能力。"""

        conflicts: list[dict[str, Any]] = []
        start, end = parse_dt(session["starts_at"]), parse_dt(session["ends_at"])
        resource_ids = [row["resource_id"] for row in connection.execute(
            "SELECT resource_id FROM session_resources WHERE session_id=?", (session["session_id"],))]
        required_languages = json.loads(session["languages_json"])
        interpreters: list[dict[str, Any]] = []
        for resource_id in resource_ids:
            resource = self._resource_row(connection, resource_id)
            blocked = connection.execute(
                "SELECT block_id, reason FROM resource_blocks WHERE resource_id=? AND starts_at < ? AND ends_at > ?",
                (resource_id, fmt_dt(end), fmt_dt(start)),
            ).fetchall()
            if blocked:
                conflicts.append({"code": "resource_blocked", "resource_id": resource_id,
                                  "kind": resource["kind"], "reason": blocked[0]["reason"]})
            competing = self._booked_sessions(connection, resource_id, start, end,
                                              exclude_session_id=session["session_id"])
            if resource["kind"] in ("room", "interpreter"):
                if competing:
                    code = "venue_unavailable" if resource["resource_id"] == session["venue_id"] \
                        else "interpreter_unavailable" if resource["kind"] == "interpreter" \
                        else "resource_unavailable"
                    conflicts.append({"code": code, "resource_id": resource_id,
                                      "competing_session_id": competing[0]["session_id"]})
            elif competing and len(competing) >= resource["capacity"]:
                conflicts.append({"code": "observer_capacity_exceeded", "resource_id": resource_id,
                                  "competing_session_id": competing[0]["session_id"]})
            if resource["kind"] == "interpreter":
                interpreters.append(resource)
        for code in required_languages:
            if not any(code in {lang["language_code"] for lang in json.loads(item["languages_json"])}
                       for item in interpreters):
                conflicts.append({"code": "interpreter_language_missing", "language_code": code})
        return conflicts

    def _participant_conflicts(self, connection, representative_id: str,
                               session: dict[str, Any]) -> list[dict[str, Any]]:
        """检测代表个人的时间、互斥与回避冲突。"""

        conflicts: list[dict[str, Any]] = []
        candidate = self._rep_row(connection, representative_id)
        mine = connection.execute(
            "SELECT se.* FROM seats s JOIN sessions se ON se.session_id=s.session_id "
            "WHERE s.representative_id=? AND s.state IN ('invited','accepted')",
            (representative_id,),
        ).fetchall()
        start, end = parse_dt(session["starts_at"]), parse_dt(session["ends_at"])
        for row in mine:
            other = dict(row)
            if other["session_id"] == session["session_id"]:
                continue
            pair = connection.execute(
                "SELECT 1 FROM session_exclusions WHERE "
                "((session_id_a=? AND session_id_b=?) OR (session_id_a=? AND session_id_b=?))",
                (session["session_id"], other["session_id"], other["session_id"], session["session_id"]),
            ).fetchone()
            if pair:
                conflicts.append({"code": "exclusive_session", "competing_session_id": other["session_id"]})
                continue
            if other["exclusive_group"] and other["exclusive_group"] == session["exclusive_group"]:
                conflicts.append({"code": "exclusive_group", "exclusive_group": other["exclusive_group"],
                                  "competing_session_id": other["session_id"]})
                continue
            if overlaps(start, end, parse_dt(other["starts_at"]), parse_dt(other["ends_at"])):
                conflicts.append({"code": "time_overlap", "competing_session_id": other["session_id"]})
        participants = [row["representative_id"] for row in connection.execute(
            "SELECT representative_id FROM seats WHERE session_id=? AND state IN ('invited','accepted')",
            (session["session_id"],))]
        recusal = self._recusal_conflict(connection, representative_id, participants)
        if recusal:
            conflicts.append(recusal)
        return conflicts

    def evaluate_conflicts(self, connection, representative_id: str, session: dict[str, Any]) -> list[dict[str, Any]]:
        """汇总资格、个人排期与资源保障三类冲突。"""

        now = self._now()
        conflicts: list[dict[str, Any]] = []
        rep = self._rep_row(connection, representative_id)
        eligibility = self.check_eligibility(connection, rep, session)
        if not eligibility.eligible:
            for reason in eligibility.reasons:
                conflicts.append({"code": reason})
        if session["status"] not in ACTIVE_SESSION_STATUSES:
            conflicts.append({"code": "session_not_active", "status": session["status"]})
        elif now >= parse_dt(session["starts_at"]):
            conflicts.append({"code": "session_started"})
        else:
            if self._effective_count(connection, session["session_id"]) >= session["capacity"]:
                conflicts.append({"code": "capacity", "capacity": session["capacity"]})
        conflicts.extend(self._participant_conflicts(connection, representative_id, session))
        conflicts.extend(self._resource_conflicts(connection, session))
        return conflicts

    def preview_conflicts(self, *, actor_id: str, session_id: str, representative_id: str) -> list[dict[str, Any]]:
        """不写入的冲突预检，供联络人向被拒代表解释。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            session = self._session_row(connection, session_id)
            rep = self._rep_row(connection, representative_id)
            self._authorize_read(connection, actor, rep["delegation_id"])
            return self.evaluate_conflicts(connection, representative_id, session)

    # ------------------------------------------------------------ 邀请与候补

    def _next_waitlist_rank(self, connection, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(waitlist_rank),0)+1 AS rank FROM seats WHERE session_id=?", (session_id,)
        ).fetchone()
        return int(row["rank"])

    def _effective_count(self, connection, session_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS c FROM seats WHERE session_id=? AND state IN ('invited','accepted')",
            (session_id,),
        ).fetchone()["c"]

    def invite(self, *, request_id: str, actor_id: str, session_id: str, representative_id: str,
               hold_minutes: int | None = None) -> dict[str, Any]:
        """发出邀请；容量不足进入稳定候补，硬冲突直接拒绝并记录原因。"""

        payload = {"actor_id": actor_id, "session_id": session_id,
                   "representative_id": representative_id, "hold_minutes": hold_minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            session = self._session_row(connection, session_id)
            rep = self._rep_row(connection, representative_id)
            self._run_due(connection)

            def create():
                existing = connection.execute(
                    "SELECT * FROM seats WHERE session_id=? AND representative_id=?",
                    (session_id, representative_id)).fetchone()
                if existing and existing["state"] in ("invited", "accepted", "waitlisted"):
                    raise ConflictError(f"代表已有席位 {existing['seat_id']}，当前状态 {existing['state']}")
                if existing and existing["state"] == "delegated":
                    raise ConflictError("席位已转授权，需先撤销转授权或邀请替代代表")
                conflicts = self.evaluate_conflicts(connection, representative_id, session)
                hard = [c for c in conflicts if c["code"] != "capacity"]
                if hard:
                    decision_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO invitation_decisions(request_id,session_id,representative_id,decision,"
                        "conflicts_json,seat_id,created_at) VALUES(?,?,?, 'rejected', ?,?,?)",
                        (decision_id, session_id, representative_id, canonical_json(hard),
                         existing["seat_id"] if existing else None, self._now_text()),
                    )
                    self._audit(connection, actor_id=actor_id, action="invitation.rejected",
                                resource_type="session", resource_id=session_id,
                                detail={"representative_id": representative_id, "conflicts": hard,
                                        "decision_id": decision_id})
                    return "invitation_decision", decision_id, {
                        "decision": "rejected", "session_id": session_id,
                        "representative_id": representative_id, "conflicts": hard,
                        "decision_id": decision_id}
                now = self._now()
                if self._effective_count(connection, session_id) >= session["capacity"]:
                    state = "waitlisted"
                    rank = self._next_waitlist_rank(connection, session_id)
                    hold_expires = None
                    decision = "waitlisted"
                else:
                    ttl = hold_minutes if hold_minutes is not None else self.hold_minutes
                    if not isinstance(ttl, int) or ttl < 1:
                        raise ValidationError("hold_minutes 必须是不小于 1 的分钟数")
                    state, rank, hold_expires = "invited", None, fmt_dt(now + timedelta(minutes=ttl))
                    decision = "invited"
                # 终态席位（谢绝/退出/超时关闭）允许重新邀请：沿用同一席位追加版本，
                # 历史保留完整轨迹；否则新建席位。
                if existing:
                    seat_id = existing["seat_id"]
                    self._write_seat_version(connection, seat_id, session_id, representative_id,
                                            rep["delegation_id"], state, rank, hold_expires,
                                            actor_id, f"invitation.{decision}_reinvite")
                else:
                    seat_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO seats(seat_id,session_id,representative_id,delegation_id,state,version,"
                        "waitlist_rank,hold_expires_at,created_at) VALUES(?,?,?,?,?,1,?,?,?)",
                        (seat_id, session_id, representative_id, rep["delegation_id"], state, rank,
                         hold_expires, self._now_text()),
                    )
                    self._write_seat_version(connection, seat_id, session_id, representative_id,
                                            rep["delegation_id"], state, rank, hold_expires,
                                            actor_id, f"invitation.{decision}")
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO invitation_decisions(request_id,session_id,representative_id,decision,"
                    "conflicts_json,seat_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (decision_id, session_id, representative_id, decision,
                     canonical_json([]), seat_id, self._now_text()),
                )
                self._reconcile_materials(connection, session_id, actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action=f"invitation.{decision}",
                            resource_type="seat", resource_id=seat_id,
                            detail={"session_id": session_id, "representative_id": representative_id,
                                    "state": state, "waitlist_rank": rank,
                                    "hold_expires_at": hold_expires, "decision_id": decision_id,
                                    "reinvited": bool(existing)})
                return "seat", seat_id, {"decision": decision, "session_id": session_id,
                                         "representative_id": representative_id, "seat_id": seat_id,
                                         "waitlist_rank": rank, "hold_expires_at": hold_expires,
                                         "conflicts": [], "decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id, action="invite",
                                    payload=payload, create=create)

    def get_decision(self, decision_id: str, *, actor_id: str) -> dict[str, Any]:
        """取回邀请结论与冲突明细，用于向被拒代表解释。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute("SELECT * FROM invitation_decisions WHERE request_id=?",
                                     (decision_id,)).fetchone()
            if row is None:
                raise NotFoundError("邀请结论不存在")
            rep = self._rep_row(connection, row["representative_id"])
            self._authorize_read(connection, actor, rep["delegation_id"])
            return {"decision_id": decision_id, "session_id": row["session_id"],
                    "representative_id": row["representative_id"], "decision": row["decision"],
                    "seat_id": row["seat_id"], "conflicts": json.loads(row["conflicts_json"]),
                    "created_at": row["created_at"]}

    # ------------------------------------------------------------ 席位状态机

    def _write_seat_version(self, connection, seat_id, session_id, representative_id, delegation_id,
                            state, rank, hold_expires, actor_id, reason, *,
                            linked_seat_id=None, replaces_seat_id=None, authorization_id=None) -> None:
        version_row = connection.execute("SELECT COALESCE(MAX(version),0) AS v FROM seat_versions WHERE seat_id=?",
                                         (seat_id,)).fetchone()
        version = version_row["v"] + 1
        connection.execute(
            "UPDATE seats SET state=?, version=?, waitlist_rank=?, hold_expires_at=?, linked_seat_id=?,"
            "replaces_seat_id=COALESCE(?,replaces_seat_id), authorization_id=COALESCE(?,authorization_id) "
            "WHERE seat_id=?",
            (state, version, rank, hold_expires, linked_seat_id, replaces_seat_id, authorization_id, seat_id),
        )
        connection.execute(
            "INSERT INTO seat_versions(seat_id,version,session_id,representative_id,delegation_id,state,"
            "waitlist_rank,hold_expires_at,linked_seat_id,replaces_seat_id,authorization_id,changed_by,"
            "reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seat_id, version, session_id, representative_id, delegation_id, state, rank, hold_expires,
             linked_seat_id, replaces_seat_id, authorization_id, actor_id, reason, self._now_text()),
        )

    def _load_seat(self, connection, seat_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM seats WHERE seat_id=?", (seat_id,)).fetchone()
        if row is None:
            raise NotFoundError("席位不存在")
        return dict(row)

    def _require_seat_actor(self, connection, actor: dict[str, Any], seat: dict[str, Any]) -> None:
        """代表侧动作仅主办操作员或该代表团联络人可执行。"""

        if actor["role"] in ("admin", "operator"):
            return
        delegation = self._delegation_row(connection, seat["delegation_id"])
        if actor["actor_id"] != delegation["liaison_actor_id"]:
            raise PermissionDenied("只能操作本代表团的席位")

    def _guard_open(self, session: dict[str, Any]) -> None:
        if session["status"] not in ACTIVE_SESSION_STATUSES:
            raise ConflictError(f"场次已{session['status']}，席位状态不能再变更")

    def respond_invitation(self, *, request_id: str, actor_id: str, seat_id: str,
                           accept: bool, reason: str = "") -> dict[str, Any]:
        """代表接受或谢绝邀请；谢绝立即释放保留并触发递补。"""

        action = "accept_invitation" if accept else "decline_invitation"
        payload = {"actor_id": actor_id, "seat_id": seat_id, "accept": accept, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            seat = self._load_seat(connection, seat_id)
            self._require_seat_actor(connection, actor, seat)
            session = self._session_row(connection, seat["session_id"])
            self._guard_open(session)
            self._run_due(connection)
            seat = self._load_seat(connection, seat_id)
            now = self._now()

            def create():
                if accept:
                    if seat["state"] != "invited":
                        raise ConflictError(f"席位当前为 {seat['state']}，不能接受")
                    if seat["hold_expires_at"] and now >= parse_dt(seat["hold_expires_at"]):
                        raise ConflictError("保留期限已过，席位已进入候补递补流程")
                    self._write_seat_version(connection, seat_id, seat["session_id"], seat["representative_id"],
                                            seat["delegation_id"], "accepted", None, None,
                                            actor_id, reason or "invitation.accepted")
                    if session["status"] == "scheduled":
                        connection.execute("UPDATE sessions SET status='confirmed' WHERE session_id=?",
                                           (session["session_id"],))
                    resulting = "accepted"
                else:
                    if seat["state"] not in ("invited", "waitlisted"):
                        raise ConflictError(f"席位当前为 {seat['state']}，不能谢绝")
                    self._write_seat_version(connection, seat_id, seat["session_id"], seat["representative_id"],
                                            seat["delegation_id"], "declined", None, None,
                                            actor_id, reason or "invitation.declined")
                    resulting = "declined"
                self._reconcile_materials(connection, session["session_id"], actor_id=actor_id)
                promotions = self._promote_due(connection, session["session_id"], actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action=f"seat.{resulting}",
                            resource_type="seat", resource_id=seat_id,
                            detail={"session_id": session["session_id"], "reason": reason,
                                    "promotions": promotions})
                return "seat", seat_id, {"seat_id": seat_id, "state": resulting, "promotions": promotions}

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    def withdraw(self, *, request_id: str, actor_id: str, seat_id: str, reason: str = "") -> dict[str, Any]:
        """代表退出已确认席位；历史签到与访问事实保留。"""

        payload = {"actor_id": actor_id, "seat_id": seat_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            seat = self._load_seat(connection, seat_id)
            self._require_seat_actor(connection, actor, seat)
            session = self._session_row(connection, seat["session_id"])
            self._guard_open(session)

            def create():
                if seat["state"] not in ("invited", "accepted", "waitlisted"):
                    raise ConflictError(f"席位当前为 {seat['state']}，不能退出")
                self._write_seat_version(connection, seat_id, seat["session_id"], seat["representative_id"],
                                        seat["delegation_id"], "withdrawn", None, None,
                                        actor_id, reason or "seat.withdrawn")
                self._reconcile_materials(connection, session["session_id"], actor_id=actor_id)
                promotions = self._promote_due(connection, session["session_id"], actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="seat.withdrawn",
                            resource_type="seat", resource_id=seat_id,
                            detail={"session_id": session["session_id"], "reason": reason,
                                    "promotions": promotions})
                return "seat", seat_id, {"seat_id": seat_id, "state": "withdrawn",
                                         "promotions": promotions}

            return self._idempotent(connection, request_id=request_id, action="withdraw_seat",
                                    payload=payload, create=create)

    def delegate(self, *, request_id: str, actor_id: str, seat_id: str, to_representative_id: str,
                 reason: str = "") -> dict[str, Any]:
        """把有效席位转授权给同代表团的合格替代代表。"""

        payload = {"actor_id": actor_id, "seat_id": seat_id,
                   "to_representative_id": to_representative_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            seat = self._load_seat(connection, seat_id)
            self._require_seat_actor(connection, actor, seat)
            session = self._session_row(connection, seat["session_id"])
            self._guard_open(session)
            target = self._rep_row(connection, to_representative_id)
            if seat["state"] != "accepted":
                raise ConflictError("只有已确认席位可以转授权")
            if target["representative_id"] == seat["representative_id"]:
                raise ValidationError("不能转授权给本人")
            if target["delegation_id"] != seat["delegation_id"]:
                raise ValidationError("替代代表必须属于同一代表团")
            eligibility = self.check_eligibility(connection, target, session)
            if not eligibility.eligible:
                raise ConflictError(f"替代代表不具备参会资格：{','.join(eligibility.reasons)}")
            personal = [c for c in self._participant_conflicts(connection, to_representative_id, session)
                        if c["code"] != "recusal"]
            # _participant_conflicts 会把原代表算作回避对象的一部分，需要先排除原席位再判断，
            # 因此这里直接用移除原席位后的在场名单复核回避。
            others = [row["representative_id"] for row in connection.execute(
                "SELECT representative_id FROM seats WHERE session_id=? AND state IN ('invited','accepted')"
                " AND representative_id<>?",
                (session["session_id"], seat["representative_id"]))]
            recusal = self._recusal_conflict(connection, to_representative_id, others)
            if personal or recusal:
                all_conflicts = personal + ([recusal] if recusal else [])
                raise ConflictError("替代代表存在冲突：" + canonical_json(all_conflicts))
            duplicate = connection.execute(
                "SELECT seat_id FROM seats WHERE session_id=? AND representative_id=? AND state IN "
                "('invited','accepted','waitlisted')",
                (session["session_id"], to_representative_id),
            ).fetchone()
            if duplicate:
                raise ConflictError("替代代表在本场次已有席位")

            def create():
                authorization_id = uuid.uuid4().hex
                new_seat_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO authorizations(authorization_id,seat_id,session_id,from_representative_id,"
                    "to_representative_id,delegation_id,issued_by,issued_at) VALUES(?,?,?,?,?,?,?,?)",
                    (authorization_id, seat_id, session["session_id"], seat["representative_id"],
                     to_representative_id, seat["delegation_id"], actor_id, self._now_text()),
                )
                self._write_seat_version(connection, seat_id, seat["session_id"], seat["representative_id"],
                                        seat["delegation_id"], "delegated", None, None,
                                        actor_id, reason or "seat.delegated",
                                        authorization_id=authorization_id)
                connection.execute(
                    "INSERT INTO seats(seat_id,session_id,representative_id,delegation_id,state,version,"
                    "replaces_seat_id,authorization_id,created_at) VALUES(?,?,?,?,'accepted',1,?,?,?)",
                    (new_seat_id, session["session_id"], to_representative_id, seat["delegation_id"],
                     seat_id, authorization_id, self._now_text()),
                )
                self._write_seat_version(connection, new_seat_id, session["session_id"], to_representative_id,
                                        seat["delegation_id"], "accepted", None, None,
                                        actor_id, reason or "authorization.accepted",
                                        replaces_seat_id=seat_id, authorization_id=authorization_id)
                self._reconcile_materials(connection, session["session_id"], actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="seat.delegated",
                            resource_type="seat", resource_id=seat_id,
                            detail={"session_id": session["session_id"],
                                    "from_representative_id": seat["representative_id"],
                                    "to_representative_id": to_representative_id,
                                    "authorization_id": authorization_id, "new_seat_id": new_seat_id,
                                    "reason": reason})
                return "seat", new_seat_id, {"seat_id": new_seat_id, "state": "accepted",
                                             "authorization_id": authorization_id,
                                             "replaces_seat_id": seat_id}

            return self._idempotent(connection, request_id=request_id, action="delegate_seat",
                                    payload=payload, create=create)

    def revoke_delegation(self, *, request_id: str, actor_id: str, authorization_id: str,
                          reason: str = "") -> dict[str, Any]:
        """撤销转授权，席位回到原代表；替代代表若已产生签到或访问事实则禁止撤销。"""

        payload = {"actor_id": actor_id, "authorization_id": authorization_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute("SELECT * FROM authorizations WHERE authorization_id=?",
                                     (authorization_id,)).fetchone()
            if row is None:
                raise NotFoundError("转授权不存在")
            authorization = dict(row)
            self._require_seat_actor(connection, actor, {"delegation_id": authorization["delegation_id"]})
            session = self._session_row(connection, authorization["session_id"])
            self._guard_open(session)
            new_seat = connection.execute(
                "SELECT * FROM seats WHERE authorization_id=? AND state='accepted'",
                (authorization_id,),
            ).fetchone()
            if new_seat is None:
                raise ConflictError("转授权已不处于生效状态")
            facts = connection.execute(
                "SELECT 1 FROM occurrence_facts WHERE session_id=? AND representative_id=? LIMIT 1",
                (session["session_id"], authorization["to_representative_id"]),
            ).fetchone()
            if facts:
                raise ConflictError("替代代表已有签到或访问事实，转授权不能撤销")

            def create():
                connection.execute(
                    "UPDATE authorizations SET revoked_at=?, revoked_by=? WHERE authorization_id=?",
                    (self._now_text(), actor_id, authorization_id),
                )
                self._write_seat_version(connection, new_seat["seat_id"], session["session_id"],
                                        new_seat["representative_id"], new_seat["delegation_id"],
                                        "cancelled", None, None, actor_id,
                                        reason or "authorization.revoked")
                self._write_seat_version(connection, authorization["seat_id"], session["session_id"],
                                        authorization["from_representative_id"],
                                        authorization["delegation_id"], "accepted", None, None,
                                        actor_id, reason or "authorization.revoked_restored")
                self._reconcile_materials(connection, session["session_id"], actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="authorization.revoked",
                            resource_type="authorization", resource_id=authorization_id,
                            detail={"session_id": session["session_id"], "seat_id": authorization["seat_id"],
                                    "reason": reason})
                return "authorization", authorization_id, {"authorization_id": authorization_id,
                                                            "state": "revoked",
                                                            "seat_id": authorization["seat_id"]}

            return self._idempotent(connection, request_id=request_id, action="revoke_delegation",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 到期与递补

    def _expire_holds(self, connection) -> list[dict[str, Any]]:
        """超过保留期限或场次已开始的邀请转入候补（位置保留、不重置）；候补随开场关闭。"""

        now = self._now()
        expired: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT s.*, se.starts_at AS session_starts_at, se.status AS session_status "
            "FROM seats s JOIN sessions se ON se.session_id=s.session_id "
            "WHERE s.state IN ('invited','waitlisted') AND se.status IN ('scheduled','confirmed')",
        ).fetchall()
        for row in rows:
            seat = dict(row)
            started = now >= parse_dt(seat["session_starts_at"])
            if started:
                new_state, reason = "cancelled", "hold.session_started"
                rank, expires = None, None
            elif seat["state"] == "invited" and seat["hold_expires_at"] is not None \
                    and now >= parse_dt(seat["hold_expires_at"]):
                new_state, reason = "waitlisted", "hold.expired"
                rank = seat["waitlist_rank"] or self._next_waitlist_rank(connection, seat["session_id"])
                expires = None
            else:
                continue
            self._write_seat_version(connection, seat["seat_id"], seat["session_id"],
                                    seat["representative_id"], seat["delegation_id"],
                                    new_state, rank, expires, "system", reason)
            expired.append({"seat_id": seat["seat_id"], "session_id": seat["session_id"],
                            "state": new_state, "waitlist_rank": rank})
        return expired

    def _promote_due(self, connection, session_id: str | None = None, *, actor_id: str = "system",
                     exclude_seat_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """按稳定候补次序递补，直到席位补满；候补位置与发放期限均持久化。

        exclude_seat_ids 中的席位（通常是本次刚因保留到期回到候补的）不参与本轮递补：
        它们必须等待他人退出释放空位，而不是立即自我递补。
        """

        exclude_seat_ids = exclude_seat_ids or set()
        promotions: list[dict[str, Any]] = []
        session_ids = [session_id] if session_id else [
            row["session_id"] for row in connection.execute(
                "SELECT DISTINCT session_id FROM seats WHERE state='waitlisted'")]
        for target_id in session_ids:
            session = connection.execute("SELECT * FROM sessions WHERE session_id=?",
                                         (target_id,)).fetchone()
            if session is None or session["status"] not in ACTIVE_SESSION_STATUSES:
                continue
            if self._now() >= parse_dt(session["starts_at"]):
                continue
            while self._effective_count(connection, target_id) < session["capacity"]:
                candidate = connection.execute(
                    "SELECT * FROM seats WHERE session_id=? AND state='waitlisted' "
                    "ORDER BY waitlist_rank ASC, created_at ASC, seat_id ASC LIMIT 1",
                    (target_id,),
                ).fetchone()
                if candidate is None or candidate["seat_id"] in exclude_seat_ids:
                    break
                # 递补前再次核验资格与硬冲突；不满足则跳过但保留候补位置。
                session_dict = dict(session)
                conflicts = [c for c in self.evaluate_conflicts(connection, candidate["representative_id"],
                                                                session_dict)
                             if c["code"] != "capacity"]
                if conflicts:
                    break
                ttl = self.hold_minutes
                expires = fmt_dt(self._now() + timedelta(minutes=ttl))
                self._write_seat_version(connection, candidate["seat_id"], target_id,
                                        candidate["representative_id"], candidate["delegation_id"],
                                        "invited", None, expires, actor_id, "waitlist.promoted")
                promotions.append({"seat_id": candidate["seat_id"], "session_id": target_id,
                                   "representative_id": candidate["representative_id"],
                                   "waitlist_rank": candidate["waitlist_rank"],
                                   "hold_expires_at": expires})
        if promotions:
            affected = {item["session_id"] for item in promotions}
            for target_id in affected:
                self._reconcile_materials(connection, target_id, actor_id=actor_id)
            for item in promotions:
                self._audit(connection, actor_id=actor_id, action="waitlist.promoted",
                            resource_type="seat", resource_id=item["seat_id"], detail=item)
        return promotions

    def _run_due(self, connection) -> dict[str, Any]:
        expired = self._expire_holds(connection)
        expired_ids = {item["seat_id"] for item in expired}
        promotions = self._promote_due(connection, None, exclude_seat_ids=expired_ids)
        if expired:
            for item in expired:
                self._audit(connection, actor_id="system", action="hold.expired",
                            resource_type="seat", resource_id=item["seat_id"], detail=item)
            for target_id in {item["session_id"] for item in expired}:
                self._reconcile_materials(connection, target_id, actor_id="system")
        return {"expired": expired, "promoted": promotions}

    def run_due(self, *, request_id: str, actor_id: str) -> dict[str, Any]:
        """显式处理到期保留与递补（进程恢复后调用即可延续原节奏）。"""

        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")

            def create():
                result = self._run_due(connection)
                return "maintenance", "due", result

            return self._idempotent(connection, request_id=request_id, action="run_due",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 改期与结束

    def reschedule(self, *, request_id: str, actor_id: str, session_id: str,
                   starts_at: str, ends_at: str, timezone_name: str | None = None,
                   title: str | None = None) -> dict[str, Any]:
        """改期；若现有承诺或资源在新窗口产生硬冲突则拒绝，保证一个生效安排。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "starts_at": starts_at,
                   "ends_at": ends_at, "timezone_name": timezone_name, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            session = self._session_row(connection, session_id)
            self._guard_open(session)
            start, end = parse_dt(starts_at), parse_dt(ends_at)
            if end <= start:
                raise ValidationError("结束时间必须晚于开始时间")
            timezone_name = timezone_name or session["timezone_name"]
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                raise ValidationError("timezone_name 不是有效 IANA 时区") from None
            if end <= self._now():
                raise ValidationError("改期后的结束时间必须在未来")
            candidate = dict(session)
            candidate["starts_at"], candidate["ends_at"] = fmt_dt(start), fmt_dt(end)
            candidate["timezone_name"] = timezone_name
            # 资源在新时间窗的可用性（不考虑本场次自身占用）。
            resource_conflicts = self._resource_conflicts(connection, candidate)
            if resource_conflicts:
                raise ConflictError("新时间窗存在资源冲突：" + canonical_json(resource_conflicts))
            # 每名已确认/受邀代表在新窗口不得产生个人硬冲突。
            seat_rows = connection.execute(
                "SELECT representative_id FROM seats WHERE session_id=? AND state IN ('invited','accepted')",
                (session_id,)).fetchall()
            people_conflicts: dict[str, list[dict[str, Any]]] = {}
            for row in seat_rows:
                personal = self._participant_conflicts(connection, row["representative_id"], candidate)
                if personal:
                    people_conflicts[row["representative_id"]] = personal
            if people_conflicts:
                raise ConflictError("新时间窗与代表既有安排冲突：" + canonical_json(people_conflicts))

            def create():
                new_title = self._text(title, "title") if title is not None else session["title"]
                connection.execute(
                    "UPDATE sessions SET starts_at=?, ends_at=?, timezone_name=?, title=?, version=version+1 "
                    "WHERE session_id=?",
                    (fmt_dt(start), fmt_dt(end), timezone_name, new_title, session_id),
                )
                fields = {"starts_at": fmt_dt(start), "ends_at": fmt_dt(end),
                          "timezone_name": timezone_name, "title": new_title}
                connection.execute(
                    "INSERT INTO session_versions(session_id,version,fields_json,changed_by,changed_at)"
                    " VALUES(?,?,?,?,?)",
                    (session_id, session["version"] + 1, canonical_json(fields),
                     actor_id, self._now_text()),
                )
                # 改期构成新的生效安排：尚未确认的保留重新计时，已确认承诺不受影响。
                renewed: list[str] = []
                pending_rows = connection.execute(
                    "SELECT * FROM seats WHERE session_id=? AND state='invited'", (session_id,)).fetchall()
                for seat in pending_rows:
                    new_expires = fmt_dt(self._now() + timedelta(minutes=self.hold_minutes))
                    self._write_seat_version(connection, seat["seat_id"], session_id,
                                            seat["representative_id"], seat["delegation_id"],
                                            "invited", None, new_expires, actor_id,
                                            "session.rescheduled_hold_renewed")
                    renewed.append(seat["seat_id"])
                self._audit(connection, actor_id=actor_id, action="session.rescheduled",
                            resource_type="session", resource_id=session_id,
                            detail={"from": {"starts_at": session["starts_at"], "ends_at": session["ends_at"],
                                            "timezone_name": session["timezone_name"]},
                                    "to": fields, "renewed_holds": renewed})
                return "session", session_id, {"session_id": session_id, "version": session["version"] + 1,
                                                "renewed_holds": renewed}

            return self._idempotent(connection, request_id=request_id, action="reschedule_session",
                                    payload=payload, create=create)

    def conclude_session(self, *, request_id: str, actor_id: str, session_id: str,
                         summary: str = "") -> dict[str, Any]:
        """结束会谈：未回应邀请与候补全部关闭，既有事实与有效承诺永久保留。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            session = self._session_row(connection, session_id)
            self._guard_open(session)

            def create():
                connection.execute(
                    "UPDATE sessions SET status='concluded', version=version+1 WHERE session_id=?",
                    (session_id,),
                )
                closed: list[str] = []
                pending = connection.execute(
                    "SELECT * FROM seats WHERE session_id=? AND state IN ('invited','waitlisted')",
                    (session_id,),
                ).fetchall()
                for seat in pending:
                    self._write_seat_version(connection, seat["seat_id"], session_id,
                                            seat["representative_id"], seat["delegation_id"],
                                            "cancelled", None, None, actor_id, "session.concluded")
                    closed.append(seat["seat_id"])
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO occurrence_facts(fact_id,session_id,fact_type,detail_json,recorded_by,recorded_at)"
                    " VALUES(?,?,'meeting_concluded',?,?,?)",
                    (fact_id, session_id, canonical_json({"summary": summary, "closed_seats": closed}),
                     actor_id, self._now_text()),
                )
                self._reconcile_materials(connection, session_id, actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="session.concluded",
                            resource_type="session", resource_id=session_id,
                            detail={"summary": summary, "closed_seats": closed, "fact_id": fact_id})
                return "session", session_id, {"session_id": session_id, "status": "concluded",
                                                "closed_seats": closed, "fact_id": fact_id}

            return self._idempotent(connection, request_id=request_id, action="conclude_session",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 签到与访问事实

    def check_in(self, *, request_id: str, actor_id: str, session_id: str,
                 representative_id: str) -> dict[str, Any]:
        """记录签到事实；事实只追加，后续名单变更不能覆盖。"""

        payload = {"actor_id": actor_id, "session_id": session_id,
                   "representative_id": representative_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "observer")
            session = self._session_row(connection, session_id)
            if session["status"] not in ACTIVE_SESSION_STATUSES:
                raise ConflictError("场次未处于可签到状态")
            seat = connection.execute(
                "SELECT * FROM seats WHERE session_id=? AND representative_id=? ORDER BY version DESC LIMIT 1",
                (session_id, representative_id),
            ).fetchone()
            if seat is None:
                raise NotFoundError("代表在本场次没有席位")
            if seat["state"] not in ("invited", "accepted"):
                raise PermissionDenied(f"席位状态 {seat['state']} 不能签到")
            now = self._now()
            if not (parse_dt(session["starts_at"]) <= now <= parse_dt(session["ends_at"])):
                raise ConflictError("只能在场次时间窗内签到")

            def create():
                existing = connection.execute(
                    "SELECT fact_id FROM occurrence_facts WHERE session_id=? AND fact_type='checked_in' "
                    "AND representative_id=?",
                    (session_id, representative_id),
                ).fetchone()
                if existing:
                    return "occurrence_fact", existing["fact_id"], {"fact_id": existing["fact_id"]}
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO occurrence_facts(fact_id,session_id,fact_type,representative_id,seat_id,"
                    "detail_json,recorded_by,recorded_at) VALUES(?,?,'checked_in',?,?,?,?,?)",
                    (fact_id, session_id, representative_id, seat["seat_id"],
                     canonical_json({"seat_state": seat["state"], "commitment_version": seat["version"]}),
                     actor_id, self._now_text()),
                )
                self._audit(connection, actor_id=actor_id, action="occurrence.checked_in",
                            resource_type="session", resource_id=session_id,
                            detail={"representative_id": representative_id, "seat_id": seat["seat_id"],
                                    "fact_id": fact_id})
                return "occurrence_fact", fact_id, {"fact_id": fact_id}

            return self._idempotent(connection, request_id=request_id, action="check_in",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 材料与权限

    def upload_material(self, *, request_id: str, actor_id: str, session_id: str, material_id: str,
                        title: str, sensitivity: str, content_hash: str | None = None) -> dict[str, Any]:
        """登记会谈材料并立即按当前有效席位开放权限。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "material_id": material_id,
                   "title": title, "sensitivity": sensitivity, "content_hash": content_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            session = self._session_row(connection, session_id)
            material_id = self._id(material_id, "material_id")
            title = self._text(title, "title")
            sensitivity = self._sensitivity(sensitivity)
            if SENSITIVITY_LEVELS[sensitivity] > SENSITIVITY_LEVELS[session["sensitivity"]]:
                raise ValidationError("材料敏感级别不能高于场次敏感级别")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO materials(material_id,session_id,title,sensitivity,content_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (material_id, session_id, title, sensitivity, content_hash,
                         actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("材料编号已经存在") from exc
                grants = self._reconcile_materials(connection, session_id, actor_id=actor_id,
                                                   only_material=material_id)
                self._audit(connection, actor_id=actor_id, action="material.uploaded",
                            resource_type="material", resource_id=material_id,
                            detail={"session_id": session_id, "title": title,
                                    "sensitivity": sensitivity, "grants": grants})
                return "material", material_id, {"material_id": material_id, "grants": grants}

            return self._idempotent(connection, request_id=request_id, action="upload_material",
                                    payload=payload, create=create)

    def _material_grants(self, connection, material_id: str) -> dict[str, bool]:
        return {row["seat_id"]: bool(row["granted"]) for row in connection.execute(
            "SELECT seat_id, granted FROM material_seats WHERE material_id=?", (material_id,))}

    def _reconcile_materials(self, connection, session_id: str, *, actor_id: str,
                             only_material: str | None = None) -> int:
        """材料权限跟随有效席位与承诺版本：有效则开放，失效则收回。"""

        materials = connection.execute(
            "SELECT * FROM materials WHERE session_id=?" +
            (" AND material_id=?" if only_material else ""),
            (session_id, *([only_material] if only_material else [])),
        ).fetchall()
        seats = connection.execute("SELECT * FROM seats WHERE session_id=?", (session_id,)).fetchall()
        changes = 0
        for material in materials:
            for seat in seats:
                rep = connection.execute("SELECT * FROM representatives WHERE representative_id=?",
                                         (seat["representative_id"],)).fetchone()
                clearance_ok = rep is not None and SENSITIVITY_LEVELS[rep["clearance"]] >= \
                    SENSITIVITY_LEVELS[material["sensitivity"]]
                should_grant = seat["state"] in EFFECTIVE_SEAT_STATES and clearance_ok
                source = "authorization" if seat["authorization_id"] and seat["state"] == "accepted" else "seat"
                existing = connection.execute(
                    "SELECT granted FROM material_seats WHERE material_id=? AND seat_id=?",
                    (material["material_id"], seat["seat_id"]),
                ).fetchone()
                if existing is None or bool(existing["granted"]) != should_grant:
                    connection.execute(
                        "INSERT INTO material_seats(material_id,seat_id,granted,source,commitment_version,"
                        "updated_by,updated_at) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(material_id,seat_id) DO UPDATE SET granted=excluded.granted,"
                        "source=excluded.source,commitment_version=excluded.commitment_version,"
                        "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                        (material["material_id"], seat["seat_id"], 1 if should_grant else 0, source,
                         seat["version"], actor_id, self._now_text()),
                    )
                    changes += 1
        return changes

    def access_material(self, *, actor_id: str, material_id: str, representative_id: str) -> dict[str, Any]:
        """下载/访问材料：实时核验有效席位、知悉级别与授权；通过则留下不可变访问事实。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            material = connection.execute("SELECT * FROM materials WHERE material_id=?",
                                          (material_id,)).fetchone()
            if material is None:
                raise NotFoundError("材料不存在")
            seat = connection.execute(
                "SELECT * FROM seats WHERE session_id=? AND representative_id=? ORDER BY version DESC LIMIT 1",
                (material["session_id"], representative_id),
            ).fetchone()
            rep = self._rep_row(connection, representative_id)
            delegation = self._delegation_row(connection, rep["delegation_id"])
            if actor["role"] not in ("admin", "operator", "observer") and \
                    actor["actor_id"] != delegation["liaison_actor_id"]:
                raise PermissionDenied("无权为该代表请求材料")
            if seat is None or seat["state"] not in EFFECTIVE_SEAT_STATES:
                raise PermissionDenied("席位已失效，材料访问权已随承诺收回")
            grant = connection.execute(
                "SELECT * FROM material_seats WHERE material_id=? AND seat_id=?",
                (material_id, seat["seat_id"]),
            ).fetchone()
            if grant is None or not grant["granted"]:
                raise PermissionDenied("该席位未获得本材料授权")
            if SENSITIVITY_LEVELS[rep["clearance"]] < SENSITIVITY_LEVELS[material["sensitivity"]]:
                raise PermissionDenied("代表当前知悉级别低于材料敏感级别")
            fact_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO occurrence_facts(fact_id,session_id,fact_type,representative_id,seat_id,"
                "detail_json,recorded_by,recorded_at) VALUES(?,?,'material_accessed',?,?,?,?,?)",
                (fact_id, material["session_id"], representative_id, seat["seat_id"],
                 canonical_json({"material_id": material_id, "sensitivity": material["sensitivity"],
                                 "commitment_version": seat["version"],
                                 "content_hash": material["content_hash"]}),
                 actor_id, self._now_text()),
            )
            self._audit(connection, actor_id=actor_id, action="material.accessed",
                        resource_type="material", resource_id=material_id,
                        detail={"representative_id": representative_id, "seat_id": seat["seat_id"],
                                "fact_id": fact_id})
            return {"allowed": True, "material_id": material_id, "title": material["title"],
                    "sensitivity": material["sensitivity"], "content_hash": material["content_hash"],
                    "seat_id": seat["seat_id"], "commitment_version": seat["version"],
                    "fact_id": fact_id}

    # ------------------------------------------------------------ 查询视图

    def _authorize_read(self, connection, actor: dict[str, Any], delegation_id: str) -> None:
        if actor["role"] in ("admin", "operator", "reviewer", "auditor"):
            return
        delegation = self._delegation_row(connection, delegation_id)
        if actor["actor_id"] == delegation["liaison_actor_id"]:
            return
        raise PermissionDenied("只能查看本代表团的信息")

    def _seat_model(self, row: dict[str, Any]) -> Seat:
        return Seat(row["seat_id"], row["session_id"], row["representative_id"], row["delegation_id"],
                    row["state"], row["version"], row["waitlist_rank"], row["hold_expires_at"],
                    row["linked_seat_id"], row["replaces_seat_id"], row["authorization_id"])

    def seat_history(self, seat_id: str, *, actor_id: str) -> list[SeatHistoryItem]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            seat = self._load_seat(connection, seat_id)
            self._authorize_read(connection, actor, seat["delegation_id"])
            rows = connection.execute("SELECT * FROM seat_versions WHERE seat_id=? ORDER BY version",
                                      (seat_id,)).fetchall()
            return [SeatHistoryItem(row["version"], row["state"], row["representative_id"],
                                    row["delegation_id"], row["waitlist_rank"], row["hold_expires_at"],
                                    row["linked_seat_id"], row["replaces_seat_id"], row["authorization_id"],
                                    row["changed_by"], row["reason"], row["created_at"]) for row in rows]

    def my_schedule(self, representative_id: str, *, actor_id: str) -> list[MyScheduleItem]:
        """代表本人视角：只返回自己的席位、期限与材料。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            rep = self._rep_row(connection, representative_id)
            self._authorize_read(connection, actor, rep["delegation_id"])
            rows = connection.execute(
                "SELECT s.*, se.title, se.starts_at, se.ends_at, se.timezone_name FROM seats s "
                "JOIN sessions se ON se.session_id=s.session_id WHERE s.representative_id=? "
                "ORDER BY se.starts_at, s.seat_id",
                (representative_id,),
            ).fetchall()
            items: list[MyScheduleItem] = []
            for row in rows:
                materials = []
                for grant in connection.execute(
                        "SELECT m.material_id,m.title,m.sensitivity,ms.granted,ms.commitment_version "
                        "FROM material_seats ms JOIN materials m ON m.material_id=ms.material_id "
                        "WHERE ms.seat_id=? ORDER BY m.material_id", (row["seat_id"],)):
                    materials.append({"material_id": grant["material_id"], "title": grant["title"],
                                      "sensitivity": grant["sensitivity"], "granted": bool(grant["granted"]),
                                      "commitment_version": grant["commitment_version"]})
                items.append(MyScheduleItem(
                    row["session_id"], row["title"], row["starts_at"], row["ends_at"],
                    row["timezone_name"], row["seat_id"], row["state"], row["waitlist_rank"],
                    row["hold_expires_at"], row["state"] in EFFECTIVE_SEAT_STATES,
                    row["replaces_seat_id"], row["authorization_id"], materials))
            return items

    def resource_schedule(self, resource_id: str, *, actor_id: str) -> list[ResourceScheduleItem]:
        """资源保障方视角：只返回该资源被场次保留/确认的时间窗。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            resource = self._resource_row(connection, resource_id)
            if actor["role"] not in ("admin", "operator", "reviewer", "auditor") and \
                    actor["organization_id"] != resource["organization_id"]:
                raise PermissionDenied("只能查看本机构资源的排期")
            rows = connection.execute(
                "SELECT se.* FROM sessions se JOIN session_resources sr ON sr.session_id=se.session_id "
                "WHERE sr.resource_id=? AND se.status IN ('scheduled','confirmed','concluded') "
                "ORDER BY se.starts_at",
                (resource_id,),
            ).fetchall()
            items = []
            for row in rows:
                effective = connection.execute(
                    "SELECT COUNT(*) AS c FROM seats WHERE session_id=? AND state IN ('invited','accepted')",
                    (row["session_id"],),
                ).fetchone()["c"]
                accepted = connection.execute(
                    "SELECT COUNT(*) AS c FROM seats WHERE session_id=? AND state='accepted'",
                    (row["session_id"],),
                ).fetchone()["c"]
                items.append(ResourceScheduleItem(
                    row["session_id"], row["title"], row["starts_at"], row["ends_at"],
                    row["timezone_name"], row["sensitivity"], tuple(json.loads(row["languages_json"])),
                    held=effective > 0 and accepted == 0, booked=accepted > 0))
            return items

    def session_view(self, session_id: str, *, actor_id: str) -> SessionView:
        """联络组反查：参与者、资源、保密依据、材料、事实与历次变更。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            session_row = self._session_row(connection, session_id)
            full = actor["role"] in ("admin", "operator", "reviewer", "auditor")
            participants = []
            seat_rows = connection.execute(
                "SELECT s.*, r.display_name, r.organization_id, r.clearance, d.name AS delegation_name "
                "FROM seats s JOIN representatives r ON r.representative_id=s.representative_id "
                "JOIN delegations d ON d.delegation_id=s.delegation_id "
                "WHERE s.session_id=? ORDER BY COALESCE(s.waitlist_rank, 999999), s.state, s.seat_id",
                (session_id,)).fetchall()
            for row in seat_rows:
                visible = full or actor["actor_id"] == self._delegation_row(
                    connection, row["delegation_id"])["liaison_actor_id"]
                item = {
                    "seat_id": row["seat_id"],
                    "representative_id": row["representative_id"],
                    "delegation_id": row["delegation_id"],
                    "organization_id": row["organization_id"] if visible else None,
                    "display_name": row["display_name"] if visible else "其他代表团代表",
                    "state": row["state"],
                    "effective": row["state"] in EFFECTIVE_SEAT_STATES,
                    "waitlist_rank": row["waitlist_rank"],
                    "hold_expires_at": row["hold_expires_at"],
                    "commitment_version": row["version"],
                    "authorization_id": row["authorization_id"],
                    "replaces_seat_id": row["replaces_seat_id"],
                    "clearance": row["clearance"] if visible else None,
                }
                if item["authorization_id"] and visible:
                    auth = connection.execute(
                        "SELECT from_representative_id,to_representative_id,issued_at,revoked_at "
                        "FROM authorizations WHERE authorization_id=?",
                        (row["authorization_id"],)).fetchone()
                    item["authorization"] = dict(auth) if auth else None
                participants.append(item)
            resources = []
            for row in connection.execute(
                    "SELECT r.* FROM resources r JOIN session_resources sr ON sr.resource_id=r.resource_id "
                    "WHERE sr.session_id=? ORDER BY r.kind, r.resource_id", (session_id,)):
                own = actor["organization_id"] == row["organization_id"]
                if not (full or own):
                    continue
                resources.append({"resource_id": row["resource_id"], "kind": row["kind"],
                                  "label": row["label"], "capacity": row["capacity"],
                                  "sensitivity": row["sensitivity"], "is_venue": row["resource_id"] == session_row["venue_id"],
                                  "languages": json.loads(row["languages_json"]),
                                  "organization_id": row["organization_id"]})
            materials = []
            for row in connection.execute("SELECT * FROM materials WHERE session_id=? ORDER BY material_id",
                                         (session_id,)):
                grants = []
                for grant in connection.execute(
                        "SELECT ms.seat_id, ms.granted, ms.source, ms.commitment_version, "
                        "s.representative_id, s.delegation_id FROM material_seats ms "
                        "JOIN seats s ON s.seat_id=ms.seat_id WHERE ms.material_id=? "
                        "ORDER BY s.representative_id", (row["material_id"],)):
                    liaison = self._delegation_row(connection, grant["delegation_id"])["liaison_actor_id"]
                    if not full and actor["actor_id"] != liaison:
                        continue
                    grants.append({"seat_id": grant["seat_id"], "representative_id": grant["representative_id"],
                                   "granted": bool(grant["granted"]), "source": grant["source"],
                                   "commitment_version": grant["commitment_version"]})
                materials.append({"material_id": row["material_id"], "title": row["title"],
                                  "sensitivity": row["sensitivity"], "content_hash": row["content_hash"],
                                  "grants": grants})
            facts = []
            for row in connection.execute(
                    "SELECT * FROM occurrence_facts WHERE session_id=? ORDER BY recorded_at, fact_id",
                    (session_id,)):
                facts.append({"fact_id": row["fact_id"], "fact_type": row["fact_type"],
                              "representative_id": row["representative_id"], "seat_id": row["seat_id"],
                              "detail": json.loads(row["detail_json"]), "recorded_by": row["recorded_by"],
                              "recorded_at": row["recorded_at"]})
            # 保密依据：资格链（代表团-机构关联、知悉级别）与有效转授权。
            basis = []
            if full:
                for item in participants:
                    rep = connection.execute("SELECT * FROM representatives WHERE representative_id=?",
                                             (item["representative_id"],)).fetchone()
                    basis.append({
                        "representative_id": item["representative_id"],
                        "delegation_id": rep["delegation_id"],
                        "organization_id": rep["organization_id"],
                        "clearance": rep["clearance"],
                        "session_sensitivity": session_row["sensitivity"],
                        "clearance_sufficient": SENSITIVITY_LEVELS[rep["clearance"]] >=
                        SENSITIVITY_LEVELS[session_row["sensitivity"]],
                        "authorization_id": item["authorization_id"],
                    })
            history = []
            for row in connection.execute(
                    "SELECT version, changed_by, changed_at, fields_json FROM session_versions "
                    "WHERE session_id=? ORDER BY version", (session_id,)):
                history.append({"kind": "session", "version": row["version"], "changed_by": row["changed_by"],
                                "changed_at": row["changed_at"], "fields": json.loads(row["fields_json"])})
            for row in connection.execute(
                    "SELECT seat_id, version, changed_by, created_at AS changed_at, state, reason, "
                    "representative_id FROM seat_versions WHERE session_id=? "
                    "ORDER BY created_at, seat_id, version",
                    (session_id,)):
                history.append({"kind": "seat", "seat_id": row["seat_id"], "version": row["version"],
                                "changed_by": row["changed_by"], "changed_at": row["changed_at"],
                                "state": row["state"], "reason": row["reason"],
                                "representative_id": row["representative_id"]})
            history.sort(key=lambda item: (item["changed_at"], item.get("seat_id") or ""))
            return SessionView(self._session_model(session_row), participants, resources, materials,
                               facts, basis, history)

    def list_authorizations(self, session_id: str, *, actor_id: str) -> list[Authorization]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            session = self._session_row(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM authorizations WHERE session_id=? ORDER BY issued_at", (session_id,)).fetchall()
            result = []
            for row in rows:
                if actor["role"] not in ("admin", "operator", "reviewer", "auditor"):
                    delegation = self._delegation_row(connection, row["delegation_id"])
                    if actor["actor_id"] != delegation["liaison_actor_id"]:
                        continue
                result.append(Authorization(row["authorization_id"], row["seat_id"], row["session_id"],
                                            row["from_representative_id"], row["to_representative_id"],
                                            row["delegation_id"], row["issued_by"], row["issued_at"],
                                            row["revoked_at"], row["revoked_at"] is None))
            return result
