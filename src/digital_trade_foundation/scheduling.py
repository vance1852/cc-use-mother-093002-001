"""国际活动会谈编排服务。

在基础服务的机构、操作者、幂等回执和哈希链审计之上，提供：

- 代表团与代表登记，以机构关联、密级和跨时区可用窗口核验参会资格；
- 场次编排：议题敏感级别、保密会议室容量、翻译语言覆盖、合规观察员、
  回避关系与互斥场次；
- 邀请生命周期：邀请、候补、接受、转授权、退出、改期与截止后的稳定递补；
- 资源保留：翻译员、会议室与观察员在全部确认前只保留有限期限，过期按
  候补位次递补并重新保留；
- 材料授权：跟随有效席位与承诺版本（邀请版本 + 场次版本）发放或收回，
  已结束的会谈、签到与访问事实不可被新名单覆盖；
- 角色化视图与场次反查：参与者、资源、保密依据与历次变更均可追溯，
  被拒代表可以查询具体冲突原因。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .audit import append_event, canonical_json, digest
from .clock import Clock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService
from .storage import Database


MAX_SENSITIVITY = 4
SECURE_ROOM_MIN_SENSITIVITY = 3
SESSION_OPEN_STATUSES = ("published", "confirmed")
SEAT_HOLDING_STATUSES = ("invited", "accepted")
ACTIVE_INVITATION_STATUSES = ("invited", "waitlisted", "accepted")
CHECKIN_EARLY_SECONDS = 1800
DEFAULT_RESPONSE_TTL_SECONDS = 4 * 3600
DEFAULT_HOLD_TTL_SECONDS = 8 * 3600

MEETING_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegates (
    delegate_id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT UNIQUE REFERENCES actors(actor_id),
    display_name TEXT NOT NULL,
    clearance_level INTEGER NOT NULL CHECK(clearance_level BETWEEN 1 AND 4),
    languages_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    replaced_by TEXT REFERENCES delegates(delegate_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegate_availability (
    delegate_id TEXT NOT NULL REFERENCES delegates(delegate_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    PRIMARY KEY(delegate_id, start_at, end_at)
);
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    secure INTEGER NOT NULL CHECK(secure IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interpreters (
    interpreter_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    actor_id TEXT UNIQUE REFERENCES actors(actor_id),
    display_name TEXT NOT NULL,
    languages_json TEXT NOT NULL,
    clearance_level INTEGER NOT NULL CHECK(clearance_level BETWEEN 1 AND 4),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recusals (
    recusal_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('delegate', 'interpreter', 'actor')),
    subject_id TEXT NOT NULL,
    counterparty_organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    reason TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    topic TEXT NOT NULL,
    sensitivity_level INTEGER NOT NULL CHECK(sensitivity_level BETWEEN 1 AND 4),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    exclusion_group TEXT,
    required_languages_json TEXT NOT NULL,
    response_ttl_seconds INTEGER NOT NULL CHECK(response_ttl_seconds > 0),
    hold_ttl_seconds INTEGER NOT NULL CHECK(hold_ttl_seconds > 0),
    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'confirmed', 'completed', 'cancelled')),
    version INTEGER NOT NULL CHECK(version >= 1),
    hold_expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_staff (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    staff_type TEXT NOT NULL CHECK(staff_type IN ('interpreter', 'observer')),
    staff_id TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, staff_type, staff_id)
);
CREATE TABLE IF NOT EXISTS resource_holds (
    hold_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('room', 'interpreter', 'observer')),
    resource_id TEXT NOT NULL,
    language TEXT,
    status TEXT NOT NULL CHECK(status IN ('held', 'confirmed', 'released', 'expired')),
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_holds_active
    ON resource_holds(session_id, resource_type, resource_id) WHERE status IN ('held', 'confirmed');
CREATE TABLE IF NOT EXISTS invitations (
    invitation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    delegate_id TEXT NOT NULL REFERENCES delegates(delegate_id),
    status TEXT NOT NULL CHECK(status IN
        ('invited', 'waitlisted', 'accepted', 'declined', 'withdrawn', 'delegated', 'expired', 'revoked')),
    position INTEGER,
    response_due_at TEXT,
    supersedes TEXT REFERENCES invitations(invitation_id),
    invited_by TEXT NOT NULL,
    invited_at TEXT NOT NULL,
    responded_at TEXT,
    note TEXT,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invitations_active
    ON invitations(session_id, delegate_id) WHERE status IN ('invited', 'waitlisted', 'accepted');
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    title TEXT NOT NULL,
    sensitivity_level INTEGER NOT NULL CHECK(sensitivity_level BETWEEN 1 AND 4),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_grants (
    grant_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    delegate_id TEXT NOT NULL REFERENCES delegates(delegate_id),
    basis_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_material_grants_active
    ON material_grants(material_id, delegate_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS material_accesses (
    access_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    delegate_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    accessed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    checkin_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    delegate_id TEXT NOT NULL,
    checked_in_at TEXT NOT NULL,
    UNIQUE(session_id, delegate_id)
);
CREATE TABLE IF NOT EXISTS rejections (
    rejection_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    delegate_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class MeetingService(DomainService):
    """在基础服务之上编排国际活动会谈。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        self.database.connection.executescript(MEETING_SCHEMA)

    # ---------- 时间与字段工具 ----------

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0)

    def _iso(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _moment(self) -> str:
        return self._iso(self._now_dt())

    def _parse_instant(self, value: Any, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from None
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    def _languages(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{field} 必须是非空语言列表")
        result: list[str] = []
        for item in value:
            code = str(item).strip().lower()
            if not code or len(code) > 10:
                raise ValidationError(f"{field} 含有无效语言代码")
            if code not in result:
                result.append(code)
        return result

    def _level(self, value: Any, field: str) -> int:
        try:
            level = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是 1-4 的整数") from None
        if not 1 <= level <= MAX_SENSITIVITY:
            raise ValidationError(f"{field} 必须在 1 到 4 之间")
        return level

    def _ttl(self, value: Any, field: str) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是正整数秒") from None
        if seconds <= 0:
            raise ValidationError(f"{field} 必须是正整数秒")
        return seconds

    # ---------- 幂等回执 ----------

    def _replay(self, connection, *, request_id: str, action: str,
                payload: dict[str, Any]) -> tuple[WriteReceipt | None, str, str]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None, request_id, payload_hash
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), request_id, payload_hash

    def _store_receipt(self, connection, *, request_id: str, action: str, payload_hash: str,
                       resource_type: str, resource_id: str) -> WriteReceipt:
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json({resource_type: resource_id}), self._moment()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ---------- 数据装载 ----------

    def _load_delegation(self, connection, delegation_id: str):
        row = connection.execute("SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        if row is None:
            raise NotFoundError("代表团不存在")
        return row

    def _load_delegate(self, connection, delegate_id: str):
        row = connection.execute("SELECT * FROM delegates WHERE delegate_id=?", (delegate_id,)).fetchone()
        if row is None:
            raise NotFoundError("代表不存在")
        return row

    def _load_room(self, connection, room_id: str):
        row = connection.execute("SELECT * FROM rooms WHERE room_id=?", (room_id,)).fetchone()
        if row is None:
            raise NotFoundError("会议室不存在")
        return row

    def _load_interpreter(self, connection, interpreter_id: str):
        row = connection.execute("SELECT * FROM interpreters WHERE interpreter_id=?", (interpreter_id,)).fetchone()
        if row is None:
            raise NotFoundError("翻译员不存在")
        return row

    def _load_session(self, connection, session_id: str):
        row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("场次不存在")
        return row

    def _load_invitation(self, connection, invitation_id: str):
        row = connection.execute("SELECT * FROM invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
        if row is None:
            raise NotFoundError("邀请不存在")
        return row

    def _load_material(self, connection, material_id: str):
        row = connection.execute("SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        return row

    def _session_staff(self, connection, session_id: str) -> list:
        return connection.execute(
            "SELECT * FROM session_staff WHERE session_id=? ORDER BY staff_type, staff_id", (session_id,)
        ).fetchall()

    def _session_participants(self, connection, session_id: str) -> list:
        """返回当前持有有效承诺（已邀请或已接受）的参会者及其机构。"""

        return connection.execute(
            "SELECT i.delegate_id AS delegate_id, g.organization_id AS organization_id "
            "FROM invitations i JOIN delegates d ON d.delegate_id=i.delegate_id "
            "JOIN delegations g ON g.delegation_id=d.delegation_id "
            "WHERE i.session_id=? AND i.status IN ('invited','accepted')",
            (session_id,),
        ).fetchall()

    # ---------- 代表团与代表 ----------

    def register_delegation(self, *, request_id: str, actor_id: str, delegation_id: str,
                            organization_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "delegation_id": delegation_id,
                   "organization_id": organization_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            delegation_id = self._identifier(delegation_id, "delegation_id")
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="register_delegation", payload=payload)
            if replay:
                return replay
            try:
                connection.execute(
                    "INSERT INTO delegations(delegation_id,organization_id,name,active,created_at) VALUES(?,?,?,1,?)",
                    (delegation_id, organization_id, name, self._moment()))
            except Exception as exc:
                raise ConflictError("代表团编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="delegation.registered",
                         resource_type="delegation", resource_id=delegation_id,
                         detail={"organization_id": organization_id, "name": name}, occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="register_delegation",
                                       payload_hash=payload_hash, resource_type="delegation",
                                       resource_id=delegation_id)

    def _availability_windows(self, value: Any) -> list[tuple[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("availability 必须是时间窗口列表")
        windows: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValidationError("availability 窗口必须是对象")
            start = self._parse_instant(item.get("start_at", ""), f"availability[{index}].start_at")
            end = self._parse_instant(item.get("end_at", ""), f"availability[{index}].end_at")
            if end <= start:
                raise ValidationError("availability 窗口结束时间必须晚于开始时间")
            pair = (self._iso(start), self._iso(end))
            if pair not in windows:
                windows.append(pair)
        return windows

    def _insert_delegate(self, connection, *, delegate_id: str, delegation_id: str, display_name: str,
                         clearance_level: int, languages: list[str], actor_link: str | None,
                         windows: list[tuple[str, str]]) -> None:
        try:
            connection.execute(
                "INSERT INTO delegates(delegate_id,delegation_id,actor_id,display_name,clearance_level,"
                "languages_json,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (delegate_id, delegation_id, actor_link, display_name, clearance_level,
                 canonical_json(languages), self._moment()))
        except Exception as exc:
            raise ConflictError("代表编号已经存在或关联操作者被占用") from exc
        for start_at, end_at in windows:
            connection.execute(
                "INSERT INTO delegate_availability(delegate_id,start_at,end_at) VALUES(?,?,?)",
                (delegate_id, start_at, end_at))

    def register_delegate(self, *, request_id: str, actor_id: str, delegate_id: str, delegation_id: str,
                          display_name: str, clearance_level: Any, languages: Any,
                          actor_link: str | None = None, availability: Any = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "delegate_id": delegate_id, "delegation_id": delegation_id,
                   "display_name": display_name, "clearance_level": clearance_level, "languages": languages,
                   "actor_link": actor_link, "availability": availability}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "liaison")
            delegation = self._load_delegation(connection, delegation_id)
            if actor.role == "liaison" and actor.organization_id != delegation["organization_id"]:
                raise PermissionDenied("只能登记本机构代表团的代表")
            delegate_id = self._identifier(delegate_id, "delegate_id")
            display_name = self._text(display_name, "display_name")
            level = self._level(clearance_level, "clearance_level")
            language_list = self._languages(languages, "languages")
            windows = self._availability_windows(availability)
            if actor_link is not None:
                linked = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_link,)).fetchone()
                if linked is None:
                    raise NotFoundError("关联操作者不存在")
                if linked["role"] != "delegate":
                    raise ValidationError("关联操作者必须是 delegate 角色")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="register_delegate", payload=payload)
            if replay:
                return replay
            self._insert_delegate(connection, delegate_id=delegate_id, delegation_id=delegation_id,
                                  display_name=display_name, clearance_level=level,
                                  languages=language_list, actor_link=actor_link, windows=windows)
            append_event(connection, actor_id=actor_id, action="delegate.registered",
                         resource_type="delegate", resource_id=delegate_id,
                         detail={"delegation_id": delegation_id, "display_name": display_name,
                                 "clearance_level": level, "languages": language_list},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="register_delegate",
                                       payload_hash=payload_hash, resource_type="delegate",
                                       resource_id=delegate_id)

    def replace_delegate(self, *, request_id: str, actor_id: str, delegate_id: str, new_delegate_id: str,
                         display_name: str, clearance_level: Any, languages: Any,
                         availability: Any = None) -> WriteReceipt:
        """临时替换代表：旧代表停用，其有效邀请与材料授权立即收回。"""

        payload = {"actor_id": actor_id, "delegate_id": delegate_id, "new_delegate_id": new_delegate_id,
                   "display_name": display_name, "clearance_level": clearance_level,
                   "languages": languages, "availability": availability}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "liaison")
            self._sweep_locked(connection)
            old = self._load_delegate(connection, delegate_id)
            delegation = self._load_delegation(connection, old["delegation_id"])
            if actor.role == "liaison" and actor.organization_id != delegation["organization_id"]:
                raise PermissionDenied("只能替换本机构代表团的代表")
            if not old["active"]:
                raise ConflictError("代表已经停用")
            new_delegate_id = self._identifier(new_delegate_id, "new_delegate_id")
            display_name = self._text(display_name, "display_name")
            level = self._level(clearance_level, "clearance_level")
            language_list = self._languages(languages, "languages")
            windows = self._availability_windows(availability)
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="replace_delegate", payload=payload)
            if replay:
                return replay
            self._insert_delegate(connection, delegate_id=new_delegate_id, delegation_id=old["delegation_id"],
                                  display_name=display_name, clearance_level=level,
                                  languages=language_list, actor_link=None, windows=windows)
            connection.execute("UPDATE delegates SET active=0, replaced_by=? WHERE delegate_id=?",
                               (new_delegate_id, delegate_id))
            if old["actor_id"]:
                connection.execute("UPDATE actors SET active=0 WHERE actor_id=?", (old["actor_id"],))
            affected_sessions: set[str] = set()
            active_invitations = connection.execute(
                "SELECT * FROM invitations WHERE delegate_id=? AND status IN ('invited','waitlisted','accepted')",
                (delegate_id,)).fetchall()
            for invitation in active_invitations:
                self._transition_invitation(connection, invitation, "revoked",
                                            note="delegate_replaced", actor_id=actor_id)
                if invitation["status"] == "accepted":
                    self._revoke_grants(connection, session_id=invitation["session_id"],
                                        delegate_id=delegate_id, reason="delegate_replaced", actor_id=actor_id)
                affected_sessions.add(invitation["session_id"])
            append_event(connection, actor_id=actor_id, action="delegate.replaced",
                         resource_type="delegate", resource_id=delegate_id,
                         detail={"delegation_id": old["delegation_id"], "new_delegate_id": new_delegate_id},
                         occurred_at=self._moment())
            receipt = self._store_receipt(connection, request_id=request_id, action="replace_delegate",
                                          payload_hash=payload_hash, resource_type="delegate",
                                          resource_id=new_delegate_id)
            stats = self._empty_stats()
            for session_id in affected_sessions:
                self._reconcile_session(connection, session_id, self._moment(), stats)
            return receipt

    # ---------- 资源登记 ----------

    def register_room(self, *, request_id: str, actor_id: str, room_id: str, site_id: str,
                      name: str, capacity: Any, secure: Any = False) -> WriteReceipt:
        payload = {"actor_id": actor_id, "room_id": room_id, "site_id": site_id,
                   "name": name, "capacity": capacity, "secure": secure}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            room_id = self._identifier(room_id, "room_id")
            name = self._text(name, "name")
            try:
                seats = int(capacity)
            except (TypeError, ValueError):
                raise ValidationError("capacity 必须是正整数") from None
            if seats <= 0:
                raise ValidationError("capacity 必须是正整数")
            secure_flag = 1 if bool(secure) else 0
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="register_room", payload=payload)
            if replay:
                return replay
            try:
                connection.execute(
                    "INSERT INTO rooms(room_id,site_id,name,capacity,secure,created_at) VALUES(?,?,?,?,?,?)",
                    (room_id, site_id, name, seats, secure_flag, self._moment()))
            except Exception as exc:
                raise ConflictError("会议室编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="room.registered",
                         resource_type="room", resource_id=room_id,
                         detail={"site_id": site_id, "name": name, "capacity": seats, "secure": secure_flag},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="register_room",
                                       payload_hash=payload_hash, resource_type="room", resource_id=room_id)

    def register_interpreter(self, *, request_id: str, actor_id: str, interpreter_id: str, site_id: str,
                             display_name: str, languages: Any, clearance_level: Any,
                             actor_link: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "interpreter_id": interpreter_id, "site_id": site_id,
                   "display_name": display_name, "languages": languages,
                   "clearance_level": clearance_level, "actor_link": actor_link}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            interpreter_id = self._identifier(interpreter_id, "interpreter_id")
            display_name = self._text(display_name, "display_name")
            language_list = self._languages(languages, "languages")
            level = self._level(clearance_level, "clearance_level")
            if actor_link is not None:
                linked = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_link,)).fetchone()
                if linked is None:
                    raise NotFoundError("关联操作者不存在")
                if linked["role"] != "interpreter":
                    raise ValidationError("关联操作者必须是 interpreter 角色")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="register_interpreter", payload=payload)
            if replay:
                return replay
            try:
                connection.execute(
                    "INSERT INTO interpreters(interpreter_id,site_id,actor_id,display_name,languages_json,"
                    "clearance_level,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                    (interpreter_id, site_id, actor_link, display_name,
                     canonical_json(language_list), level, self._moment()))
            except Exception as exc:
                raise ConflictError("翻译员编号已经存在或关联操作者被占用") from exc
            append_event(connection, actor_id=actor_id, action="interpreter.registered",
                         resource_type="interpreter", resource_id=interpreter_id,
                         detail={"site_id": site_id, "display_name": display_name,
                                 "languages": language_list, "clearance_level": level},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="register_interpreter",
                                       payload_hash=payload_hash, resource_type="interpreter",
                                       resource_id=interpreter_id)

    def register_recusal(self, *, request_id: str, actor_id: str, subject_type: str, subject_id: str,
                         counterparty_organization_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "subject_type": subject_type, "subject_id": subject_id,
                   "counterparty_organization_id": counterparty_organization_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "compliance")
            if subject_type not in ("delegate", "interpreter", "actor"):
                raise ValidationError("subject_type 必须是 delegate、interpreter 或 actor")
            table = {"delegate": "delegates", "interpreter": "interpreters", "actor": "actors"}[subject_type]
            key = {"delegate": "delegate_id", "interpreter": "interpreter_id", "actor": "actor_id"}[subject_type]
            if connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (subject_id,)).fetchone() is None:
                raise NotFoundError("回避主体不存在")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (counterparty_organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            reason = self._text(reason, "reason")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="register_recusal", payload=payload)
            if replay:
                return replay
            recusal_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO recusals(recusal_id,subject_type,subject_id,counterparty_organization_id,reason,active,created_at) "
                "VALUES(?,?,?,?,?,1,?)",
                (recusal_id, subject_type, subject_id, counterparty_organization_id, reason, self._moment()))
            append_event(connection, actor_id=actor_id, action="recusal.registered",
                         resource_type="recusal", resource_id=recusal_id,
                         detail={"subject_type": subject_type, "subject_id": subject_id,
                                 "counterparty_organization_id": counterparty_organization_id, "reason": reason},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="register_recusal",
                                       payload_hash=payload_hash, resource_type="recusal", resource_id=recusal_id)

    # ---------- 场次编排 ----------

    def create_session(self, *, request_id: str, actor_id: str, session_id: str, site_id: str, topic: str,
                       sensitivity_level: Any, start_at: Any, end_at: Any, room_id: str,
                       required_languages: Any, exclusion_group: str | None = None,
                       response_ttl_seconds: Any = DEFAULT_RESPONSE_TTL_SECONDS,
                       hold_ttl_seconds: Any = DEFAULT_HOLD_TTL_SECONDS) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id, "site_id": site_id, "topic": topic,
                   "sensitivity_level": sensitivity_level, "start_at": start_at, "end_at": end_at,
                   "room_id": room_id, "required_languages": required_languages,
                   "exclusion_group": exclusion_group, "response_ttl_seconds": response_ttl_seconds,
                   "hold_ttl_seconds": hold_ttl_seconds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            try:
                ZoneInfo(site["timezone_name"])
            except Exception:
                raise ValidationError("场所时区无效") from None
            session_id = self._identifier(session_id, "session_id")
            topic = self._text(topic, "topic")
            level = self._level(sensitivity_level, "sensitivity_level")
            start = self._parse_instant(start_at, "start_at")
            end = self._parse_instant(end_at, "end_at")
            if end <= start:
                raise ValidationError("结束时间必须晚于开始时间")
            language_list = self._languages(required_languages, "required_languages")
            room = self._load_room(connection, room_id)
            if room["site_id"] != site_id:
                raise ValidationError("会议室不属于该场所")
            if level >= SECURE_ROOM_MIN_SENSITIVITY and not room["secure"]:
                raise ValidationError("该敏感级别必须使用保密会议室")
            response_ttl = self._ttl(response_ttl_seconds, "response_ttl_seconds")
            hold_ttl = self._ttl(hold_ttl_seconds, "hold_ttl_seconds")
            group = self._text(exclusion_group, "exclusion_group", 80) if exclusion_group else None
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="create_session", payload=payload)
            if replay:
                return replay
            try:
                connection.execute(
                    "INSERT INTO sessions(session_id,site_id,room_id,topic,sensitivity_level,start_at,end_at,"
                    "exclusion_group,required_languages_json,response_ttl_seconds,hold_ttl_seconds,status,version,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',1,?,?)",
                    (session_id, site_id, room_id, topic, level, self._iso(start), self._iso(end), group,
                     canonical_json(language_list), response_ttl, hold_ttl, actor_id, self._moment()))
            except Exception as exc:
                raise ConflictError("场次编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="session.created",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id, "site_id": site_id, "topic": topic,
                                 "sensitivity_level": level, "start_at": self._iso(start),
                                 "end_at": self._iso(end), "room_id": room_id,
                                 "required_languages": language_list, "exclusion_group": group},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="create_session",
                                       payload_hash=payload_hash, resource_type="session", resource_id=session_id)

    def assign_interpreter(self, *, request_id: str, actor_id: str, session_id: str,
                           interpreter_id: str, language: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id,
                   "interpreter_id": interpreter_id, "language": language}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session = self._load_session(connection, session_id)
            if session["status"] != "draft":
                raise ConflictError("仅草稿场次可以调整翻译安排")
            interpreter = self._load_interpreter(connection, interpreter_id)
            if not interpreter["active"]:
                raise ValidationError("翻译员已停用")
            if interpreter["site_id"] != session["site_id"]:
                raise ValidationError("翻译员不属于该场所")
            code = self._languages([language], "language")[0]
            if code not in json.loads(session["required_languages_json"]):
                raise ValidationError("该语言不在场次翻译需求中")
            if code not in json.loads(interpreter["languages_json"]):
                raise ValidationError("翻译员不具备该语言能力")
            if interpreter["clearance_level"] < session["sensitivity_level"]:
                raise ConflictError("翻译员密级不足以覆盖该议题")
            staff = self._session_staff(connection, session_id)
            if any(row["staff_type"] == "interpreter" and row["staff_id"] == interpreter_id for row in staff):
                raise ConflictError("翻译员已指派到该场次")
            if any(row["staff_type"] == "interpreter" and row["language"] == code for row in staff):
                raise ConflictError("该语言已有翻译员")
            for participant in self._session_participants(connection, session_id):
                hit = self._recusal_hit(connection, "interpreter", interpreter_id, participant["organization_id"])
                if hit:
                    raise ConflictError(f"翻译员与参会机构存在回避关系：{hit['reason']}")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="assign_interpreter", payload=payload)
            if replay:
                return replay
            connection.execute(
                "INSERT INTO session_staff(session_id,staff_type,staff_id,language,created_at) VALUES(?,?,?,?,?)",
                (session_id, "interpreter", interpreter_id, code, self._moment()))
            append_event(connection, actor_id=actor_id, action="interpreter.assigned",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id, "interpreter_id": interpreter_id, "language": code},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="assign_interpreter",
                                       payload_hash=payload_hash, resource_type="session",
                                       resource_id=session_id)

    def assign_observer(self, *, request_id: str, actor_id: str, session_id: str,
                        observer_actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id, "observer_actor_id": observer_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session = self._load_session(connection, session_id)
            if session["status"] != "draft":
                raise ConflictError("仅草稿场次可以调整观察员安排")
            observer = connection.execute("SELECT * FROM actors WHERE actor_id=?",
                                          (observer_actor_id,)).fetchone()
            if observer is None:
                raise NotFoundError("合规观察员不存在")
            if observer["role"] != "compliance":
                raise ValidationError("观察员必须是 compliance 角色")
            if not observer["active"]:
                raise ValidationError("合规观察员已停用")
            staff = self._session_staff(connection, session_id)
            if any(row["staff_type"] == "observer" and row["staff_id"] == observer_actor_id for row in staff):
                raise ConflictError("观察员已指派到该场次")
            for participant in self._session_participants(connection, session_id):
                hit = self._recusal_hit(connection, "actor", observer_actor_id, participant["organization_id"])
                if hit:
                    raise ConflictError(f"观察员与参会机构存在回避关系：{hit['reason']}")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="assign_observer", payload=payload)
            if replay:
                return replay
            connection.execute(
                "INSERT INTO session_staff(session_id,staff_type,staff_id,language,created_at) VALUES(?,?,?,?,?)",
                (session_id, "observer", observer_actor_id, None, self._moment()))
            append_event(connection, actor_id=actor_id, action="observer.assigned",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id, "observer_actor_id": observer_actor_id},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="assign_observer",
                                       payload_hash=payload_hash, resource_type="session",
                                       resource_id=session_id)

    def publish_session(self, *, request_id: str, actor_id: str, session_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] != "draft":
                raise ConflictError("场次不在草稿状态")
            required = set(json.loads(session["required_languages_json"]))
            covered = {row["language"] for row in self._session_staff(connection, session_id)
                       if row["staff_type"] == "interpreter"}
            missing = sorted(required - covered)
            if missing:
                raise ValidationError(f"缺少翻译覆盖：{','.join(missing)}")
            conflict = self._resource_conflict(connection, "room", session["room_id"], session)
            if conflict:
                raise ConflictError(f"会议室在该时段已被场次 {conflict} 占用")
            for staff in self._session_staff(connection, session_id):
                conflict = self._resource_conflict(connection, staff["staff_type"], staff["staff_id"], session)
                if conflict:
                    label = "翻译员" if staff["staff_type"] == "interpreter" else "合规观察员"
                    raise ConflictError(f"{label}在该时段已被场次 {conflict} 预订")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="publish_session", payload=payload)
            if replay:
                return replay
            self._create_holds(connection, session, "held")
            connection.execute("UPDATE sessions SET status='published' WHERE session_id=?", (session_id,))
            append_event(connection, actor_id=actor_id, action="session.published",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id, "hold_expires_at": self._fresh_hold_expiry(session)},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="publish_session",
                                       payload_hash=payload_hash, resource_type="session", resource_id=session_id)

    def reschedule_session(self, *, request_id: str, actor_id: str, session_id: str,
                           start_at: Any, end_at: Any) -> WriteReceipt:
        """改期：场次版本递进，已接受代表回到待确认，材料授权按旧版本收回。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] not in SESSION_OPEN_STATUSES:
                raise ConflictError("场次已结束或取消，不能改期")
            start = self._parse_instant(start_at, "start_at")
            end = self._parse_instant(end_at, "end_at")
            if end <= start:
                raise ValidationError("结束时间必须晚于开始时间")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="reschedule_session", payload=payload)
            if replay:
                return replay
            new_window = {"session_id": session_id, "start_at": self._iso(start), "end_at": self._iso(end)}
            conflict = self._resource_conflict(connection, "room", session["room_id"], new_window)
            if conflict:
                raise ConflictError(f"改期后会议室在该时段已被场次 {conflict} 占用")
            for staff in self._session_staff(connection, session_id):
                conflict = self._resource_conflict(connection, staff["staff_type"], staff["staff_id"], new_window)
                if conflict:
                    raise ConflictError(f"改期后资源在该时段已被场次 {conflict} 预订")
            now = self._moment()
            for hold in connection.execute(
                    "SELECT * FROM resource_holds WHERE session_id=? AND status IN ('held','confirmed')",
                    (session_id,)).fetchall():
                connection.execute("UPDATE resource_holds SET status='released' WHERE hold_id=?", (hold["hold_id"],))
                append_event(connection, actor_id=actor_id, action="hold.released",
                             resource_type="hold", resource_id=hold["hold_id"],
                             detail={"session_id": session_id, "resource_type": hold["resource_type"],
                                     "resource_id": hold["resource_id"], "reason": "rescheduled"},
                             occurred_at=now)
            new_version = session["version"] + 1
            connection.execute(
                "UPDATE sessions SET start_at=?, end_at=?, version=?, status='published' WHERE session_id=?",
                (self._iso(start), self._iso(end), new_version, session_id))
            updated = self._load_session(connection, session_id)
            self._create_holds(connection, updated, "held")
            due = self._iso(self._now_dt() + timedelta(seconds=session["response_ttl_seconds"]))
            reopened = connection.execute(
                "SELECT * FROM invitations WHERE session_id=? AND status IN ('accepted','invited')",
                (session_id,)).fetchall()
            for invitation in reopened:
                connection.execute(
                    "UPDATE invitations SET status='invited', response_due_at=?, responded_at=NULL, "
                    "version=version+1 WHERE invitation_id=?",
                    (due, invitation["invitation_id"]))
                append_event(connection, actor_id=actor_id, action="invitation.reopened",
                             resource_type="invitation", resource_id=invitation["invitation_id"],
                             detail={"session_id": session_id, "delegate_id": invitation["delegate_id"],
                                     "reason": "rescheduled", "response_due_at": due},
                             occurred_at=now)
            self._revoke_session_grants(connection, session_id, reason="rescheduled", actor_id=actor_id)
            append_event(connection, actor_id=actor_id, action="session.rescheduled",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id, "old_start_at": session["start_at"],
                                 "old_end_at": session["end_at"], "new_start_at": self._iso(start),
                                 "new_end_at": self._iso(end), "version": new_version},
                         occurred_at=now)
            return self._store_receipt(connection, request_id=request_id, action="reschedule_session",
                                       payload_hash=payload_hash, resource_type="session", resource_id=session_id)

    def cancel_session(self, *, request_id: str, actor_id: str, session_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] in ("completed", "cancelled"):
                raise ConflictError("场次已结束或已取消")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="cancel_session", payload=payload)
            if replay:
                return replay
            now = self._moment()
            for invitation in connection.execute(
                    "SELECT * FROM invitations WHERE session_id=? AND status IN ('invited','waitlisted','accepted')",
                    (session_id,)).fetchall():
                target = "revoked" if invitation["status"] == "accepted" else "expired"
                self._transition_invitation(connection, invitation, target,
                                            note="session_cancelled", actor_id=actor_id)
            self._revoke_session_grants(connection, session_id, reason="session_cancelled", actor_id=actor_id)
            self._release_holds(connection, session_id, reason="session_cancelled", actor_id=actor_id)
            connection.execute("UPDATE sessions SET status='cancelled', hold_expires_at=NULL WHERE session_id=?",
                               (session_id,))
            append_event(connection, actor_id=actor_id, action="session.cancelled",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id}, occurred_at=now)
            return self._store_receipt(connection, request_id=request_id, action="cancel_session",
                                       payload_hash=payload_hash, resource_type="session", resource_id=session_id)

    def complete_session(self, *, request_id: str, actor_id: str, session_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] not in SESSION_OPEN_STATUSES:
                raise ConflictError("场次不在进行中")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="complete_session", payload=payload)
            if replay:
                return replay
            self._complete_session_locked(connection, session, actor_id=actor_id, reason="operator_closed")
            return self._store_receipt(connection, request_id=request_id, action="complete_session",
                                       payload_hash=payload_hash, resource_type="session", resource_id=session_id)

    # ---------- 邀请生命周期 ----------

    def invite_delegate(self, *, request_id: str, actor_id: str, session_id: str,
                        delegate_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id, "delegate_id": delegate_id}
        outcome: tuple[Any, ...] = ()
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "liaison")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] not in SESSION_OPEN_STATUSES:
                raise ConflictError("场次未开放邀请")
            delegate = self._load_delegate(connection, delegate_id)
            delegation = self._load_delegation(connection, delegate["delegation_id"])
            if actor.role == "liaison" and actor.organization_id != delegation["organization_id"]:
                raise PermissionDenied("只能邀请本机构代表团的代表")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="invite_delegate", payload=payload)
            if replay:
                return replay
            duplicate = connection.execute(
                "SELECT 1 FROM invitations WHERE session_id=? AND delegate_id=? AND status IN ('invited','waitlisted','accepted')",
                (session_id, delegate_id)).fetchone()
            if duplicate:
                failure = ("duplicate_invitation", {"delegate_id": delegate_id})
            else:
                failure = self._eligibility_failure(connection, session, delegate)
            if failure:
                code, detail = failure
                self._record_rejection(connection, session_id=session_id, delegate_id=delegate_id,
                                       action="invite", reason_code=code, detail=detail, actor_id=actor_id)
                outcome = ("rejected", code, detail)
            else:
                invitation_id = uuid.uuid4().hex
                room = self._load_room(connection, session["room_id"])
                holders = self._seat_holders(connection, session_id)
                if holders >= room["capacity"]:
                    position = self._next_position(connection, session_id)
                    connection.execute(
                        "INSERT INTO invitations(invitation_id,session_id,delegate_id,status,position,"
                        "invited_by,invited_at,version) VALUES(?,?,?,'waitlisted',?,?,?,1)",
                        (invitation_id, session_id, delegate_id, position, actor_id, self._moment()))
                    append_event(connection, actor_id=actor_id, action="invitation.waitlisted",
                                 resource_type="invitation", resource_id=invitation_id,
                                 detail={"session_id": session_id, "delegate_id": delegate_id,
                                         "position": position}, occurred_at=self._moment())
                else:
                    due = self._iso(self._now_dt() + timedelta(seconds=session["response_ttl_seconds"]))
                    connection.execute(
                        "INSERT INTO invitations(invitation_id,session_id,delegate_id,status,response_due_at,"
                        "invited_by,invited_at,version) VALUES(?,?,?,'invited',?,?,?,1)",
                        (invitation_id, session_id, delegate_id, due, actor_id, self._moment()))
                    append_event(connection, actor_id=actor_id, action="invitation.sent",
                                 resource_type="invitation", resource_id=invitation_id,
                                 detail={"session_id": session_id, "delegate_id": delegate_id,
                                         "response_due_at": due}, occurred_at=self._moment())
                    if session["status"] == "confirmed":
                        connection.execute("UPDATE sessions SET status='published' WHERE session_id=?",
                                           (session_id,))
                        append_event(connection, actor_id=actor_id, action="session.reopened",
                                     resource_type="session", resource_id=session_id,
                                     detail={"session_id": session_id, "reason": "new_invitation"},
                                     occurred_at=self._moment())
                outcome = ("receipt", self._store_receipt(
                    connection, request_id=request_id, action="invite_delegate", payload_hash=payload_hash,
                    resource_type="invitation", resource_id=invitation_id))
        if outcome[0] == "rejected":
            raise ConflictError(f"邀请被拒绝({outcome[1]})：{canonical_json(outcome[2])}")
        return outcome[1]

    def respond_invitation(self, *, request_id: str, actor_id: str, invitation_id: str,
                           decision: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "invitation_id": invitation_id, "decision": decision}
        outcome: tuple[Any, ...] = ()
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._sweep_locked(connection)
            invitation = self._load_invitation(connection, invitation_id)
            session = self._load_session(connection, invitation["session_id"])
            delegate = self._load_delegate(connection, invitation["delegate_id"])
            delegation = self._load_delegation(connection, delegate["delegation_id"])
            if not self._invitation_actor_allowed(actor, delegation["organization_id"], delegate):
                raise PermissionDenied("当前角色不能响应该邀请")
            if decision not in ("accept", "decline"):
                raise ValidationError("decision 必须是 accept 或 decline")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="respond_invitation", payload=payload)
            if replay:
                return replay
            if invitation["status"] != "invited":
                raise ConflictError(f"邀请当前状态 {invitation['status']} 不允许确认")
            if decision == "decline":
                self._transition_invitation(connection, invitation, "declined",
                                            note=None, actor_id=actor_id)
                outcome = ("receipt", self._store_receipt(
                    connection, request_id=request_id, action="respond_invitation",
                    payload_hash=payload_hash, resource_type="invitation", resource_id=invitation_id))
            else:
                failure = self._eligibility_failure(connection, session, delegate)
                if failure:
                    code, detail = failure
                    self._record_rejection(connection, session_id=session["session_id"],
                                           delegate_id=delegate["delegate_id"], action="accept",
                                           reason_code=code, detail=detail, actor_id=actor_id)
                    outcome = ("rejected", code, detail)
                else:
                    connection.execute(
                        "UPDATE invitations SET status='accepted', responded_at=?, version=version+1 "
                        "WHERE invitation_id=?", (self._moment(), invitation_id))
                    append_event(connection, actor_id=actor_id, action="invitation.accepted",
                                 resource_type="invitation", resource_id=invitation_id,
                                 detail={"session_id": session["session_id"],
                                         "delegate_id": delegate["delegate_id"],
                                         "invitation_version": invitation["version"] + 1},
                                 occurred_at=self._moment())
                    self._issue_grants(connection, session, invitation_id,
                                       invitation["version"] + 1, delegate, actor_id)
                    outcome = ("receipt", self._store_receipt(
                        connection, request_id=request_id, action="respond_invitation",
                        payload_hash=payload_hash, resource_type="invitation", resource_id=invitation_id))
            if outcome[0] == "receipt":
                self._reconcile_session(connection, session["session_id"], self._moment(), self._empty_stats())
        if outcome[0] == "rejected":
            raise ConflictError(f"确认被拒绝({outcome[1]})：{canonical_json(outcome[2])}")
        return outcome[1]

    def delegate_invitation(self, *, request_id: str, actor_id: str, invitation_id: str,
                            target_delegate_id: str) -> WriteReceipt:
        """转授权：把有效邀请转给同一代表团的另一名代表。"""

        payload = {"actor_id": actor_id, "invitation_id": invitation_id,
                   "target_delegate_id": target_delegate_id}
        outcome: tuple[Any, ...] = ()
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._sweep_locked(connection)
            invitation = self._load_invitation(connection, invitation_id)
            session = self._load_session(connection, invitation["session_id"])
            if session["status"] not in SESSION_OPEN_STATUSES:
                raise ConflictError("场次未开放，不能转授权")
            source = self._load_delegate(connection, invitation["delegate_id"])
            delegation = self._load_delegation(connection, source["delegation_id"])
            if not self._invitation_actor_allowed(actor, delegation["organization_id"], source):
                raise PermissionDenied("当前角色不能转授权该邀请")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="delegate_invitation", payload=payload)
            if replay:
                return replay
            if invitation["status"] not in ("invited", "accepted"):
                raise ConflictError(f"邀请当前状态 {invitation['status']} 不允许转授权")
            target = self._load_delegate(connection, target_delegate_id)
            if target["delegation_id"] != source["delegation_id"]:
                raise ValidationError("转授权只能交给同一代表团成员")
            duplicate = connection.execute(
                "SELECT 1 FROM invitations WHERE session_id=? AND delegate_id=? AND status IN ('invited','waitlisted','accepted')",
                (session["session_id"], target_delegate_id)).fetchone()
            if duplicate:
                failure = ("duplicate_invitation", {"delegate_id": target_delegate_id})
            else:
                failure = self._eligibility_failure(connection, session, target)
            if failure:
                code, detail = failure
                self._record_rejection(connection, session_id=session["session_id"],
                                       delegate_id=target_delegate_id, action="delegate",
                                       reason_code=code, detail=detail, actor_id=actor_id)
                outcome = ("rejected", code, detail)
            else:
                now = self._moment()
                connection.execute(
                    "UPDATE invitations SET status='delegated', responded_at=?, version=version+1 "
                    "WHERE invitation_id=?", (now, invitation_id))
                new_invitation_id = uuid.uuid4().hex
                if invitation["status"] == "accepted":
                    connection.execute(
                        "INSERT INTO invitations(invitation_id,session_id,delegate_id,status,supersedes,"
                        "invited_by,invited_at,responded_at,version) VALUES(?,?,?,'accepted',?,?,?,?,1)",
                        (new_invitation_id, session["session_id"], target_delegate_id, invitation_id,
                         actor_id, now, now))
                    self._revoke_grants(connection, session_id=session["session_id"],
                                        delegate_id=source["delegate_id"], reason="delegated",
                                        actor_id=actor_id)
                    self._issue_grants(connection, session, new_invitation_id, 1, target, actor_id)
                else:
                    due = self._iso(self._now_dt() + timedelta(seconds=session["response_ttl_seconds"]))
                    connection.execute(
                        "INSERT INTO invitations(invitation_id,session_id,delegate_id,status,response_due_at,"
                        "supersedes,invited_by,invited_at,version) VALUES(?,?,?,'invited',?,?,?,?,1)",
                        (new_invitation_id, session["session_id"], target_delegate_id, due, invitation_id,
                         actor_id, now))
                append_event(connection, actor_id=actor_id, action="invitation.delegated",
                             resource_type="invitation", resource_id=invitation_id,
                             detail={"session_id": session["session_id"],
                                     "from_delegate_id": source["delegate_id"],
                                     "to_delegate_id": target_delegate_id,
                                     "new_invitation_id": new_invitation_id}, occurred_at=now)
                self._reconcile_session(connection, session["session_id"], now, self._empty_stats())
                outcome = ("receipt", self._store_receipt(
                    connection, request_id=request_id, action="delegate_invitation",
                    payload_hash=payload_hash, resource_type="invitation", resource_id=new_invitation_id))
        if outcome[0] == "rejected":
            raise ConflictError(f"转授权被拒绝({outcome[1]})：{canonical_json(outcome[2])}")
        return outcome[1]

    def withdraw_invitation(self, *, request_id: str, actor_id: str, invitation_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "invitation_id": invitation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._sweep_locked(connection)
            invitation = self._load_invitation(connection, invitation_id)
            session = self._load_session(connection, invitation["session_id"])
            delegate = self._load_delegate(connection, invitation["delegate_id"])
            delegation = self._load_delegation(connection, delegate["delegation_id"])
            if not self._invitation_actor_allowed(actor, delegation["organization_id"], delegate):
                raise PermissionDenied("当前角色不能退出该邀请")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="withdraw_invitation", payload=payload)
            if replay:
                return replay
            if invitation["status"] not in ACTIVE_INVITATION_STATUSES:
                raise ConflictError(f"邀请当前状态 {invitation['status']} 不允许退出")
            was_accepted = invitation["status"] == "accepted"
            self._transition_invitation(connection, invitation, "withdrawn",
                                        note=None, actor_id=actor_id)
            if was_accepted:
                self._revoke_grants(connection, session_id=session["session_id"],
                                    delegate_id=delegate["delegate_id"], reason="withdrawn",
                                    actor_id=actor_id)
            receipt = self._store_receipt(connection, request_id=request_id, action="withdraw_invitation",
                                          payload_hash=payload_hash, resource_type="invitation",
                                          resource_id=invitation_id)
            self._reconcile_session(connection, session["session_id"], self._moment(), self._empty_stats())
            return receipt

    # ---------- 材料与签到 ----------

    def add_material(self, *, request_id: str, actor_id: str, material_id: str, session_id: str,
                     title: str, sensitivity_level: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "material_id": material_id, "session_id": session_id,
                   "title": title, "sensitivity_level": sensitivity_level}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            if session["status"] in ("completed", "cancelled"):
                raise ConflictError("场次已结束，不能新增材料")
            material_id = self._identifier(material_id, "material_id")
            title = self._text(title, "title")
            level = self._level(sensitivity_level, "sensitivity_level")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="add_material", payload=payload)
            if replay:
                return replay
            try:
                connection.execute(
                    "INSERT INTO materials(material_id,session_id,title,sensitivity_level,version,created_at) "
                    "VALUES(?,?,?,?,1,?)",
                    (material_id, session_id, title, level, self._moment()))
            except Exception as exc:
                raise ConflictError("材料编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="material.added",
                         resource_type="material", resource_id=material_id,
                         detail={"session_id": session_id, "title": title, "sensitivity_level": level},
                         occurred_at=self._moment())
            holders = connection.execute(
                "SELECT i.invitation_id, i.version, d.* FROM invitations i "
                "JOIN delegates d ON d.delegate_id=i.delegate_id "
                "WHERE i.session_id=? AND i.status='accepted'", (session_id,)).fetchall()
            for holder in holders:
                if holder["clearance_level"] >= level:
                    self._issue_grants(connection, session, holder["invitation_id"],
                                       holder["version"], holder, actor_id)
            return self._store_receipt(connection, request_id=request_id, action="add_material",
                                       payload_hash=payload_hash, resource_type="material",
                                       resource_id=material_id)

    def access_material(self, *, request_id: str, actor_id: str, material_id: str) -> WriteReceipt:
        """代表下载材料：必须持有跟随当前承诺版本的有效授权，访问事实不可覆盖。"""

        payload = {"actor_id": actor_id, "material_id": material_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            material = self._load_material(connection, material_id)
            session = self._load_session(connection, material["session_id"])
            delegate = connection.execute("SELECT * FROM delegates WHERE actor_id=?",
                                          (actor_id,)).fetchone()
            if delegate is None:
                raise PermissionDenied("仅代表本人可以访问材料")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="access_material", payload=payload)
            if replay:
                return replay
            grant = connection.execute(
                "SELECT * FROM material_grants WHERE material_id=? AND delegate_id=? AND status='active'",
                (material_id, delegate["delegate_id"])).fetchone()
            if grant is None:
                raise PermissionDenied("没有有效材料授权")
            basis = json.loads(grant["basis_json"])
            if basis.get("session_version") != session["version"]:
                raise PermissionDenied("授权对应的承诺版本已失效")
            access_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO material_accesses(access_id,material_id,session_id,delegate_id,grant_id,accessed_at) "
                "VALUES(?,?,?,?,?,?)",
                (access_id, material_id, session["session_id"], delegate["delegate_id"],
                 grant["grant_id"], self._moment()))
            append_event(connection, actor_id=actor_id, action="material.accessed",
                         resource_type="material_access", resource_id=access_id,
                         detail={"session_id": session["session_id"], "material_id": material_id,
                                 "delegate_id": delegate["delegate_id"], "grant_id": grant["grant_id"]},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="access_material",
                                       payload_hash=payload_hash, resource_type="material_access",
                                       resource_id=access_id)

    def check_in(self, *, request_id: str, actor_id: str, session_id: str,
                 delegate_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "session_id": session_id, "delegate_id": delegate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._sweep_locked(connection)
            session = self._load_session(connection, session_id)
            delegate = self._load_delegate(connection, delegate_id)
            delegation = self._load_delegation(connection, delegate["delegation_id"])
            if not self._invitation_actor_allowed(actor, delegation["organization_id"], delegate):
                raise PermissionDenied("当前角色不能为该代表签到")
            replay, request_id, payload_hash = self._replay(
                connection, request_id=request_id, action="check_in", payload=payload)
            if replay:
                return replay
            if session["status"] not in SESSION_OPEN_STATUSES:
                raise ConflictError("场次未开放签到")
            seat = connection.execute(
                "SELECT 1 FROM invitations WHERE session_id=? AND delegate_id=? AND status='accepted'",
                (session_id, delegate_id)).fetchone()
            if seat is None:
                raise ConflictError("代表未持有有效席位")
            now_dt = self._now_dt()
            start = self._parse_instant(session["start_at"], "start_at")
            end = self._parse_instant(session["end_at"], "end_at")
            if not (start - timedelta(seconds=CHECKIN_EARLY_SECONDS) <= now_dt <= end):
                raise ConflictError("不在签到时间窗口内")
            checkin_id = uuid.uuid4().hex
            try:
                connection.execute(
                    "INSERT INTO checkins(checkin_id,session_id,delegate_id,checked_in_at) VALUES(?,?,?,?)",
                    (checkin_id, session_id, delegate_id, self._moment()))
            except Exception as exc:
                raise ConflictError("代表已签到，不能重复登记") from exc
            append_event(connection, actor_id=actor_id, action="checkin.recorded",
                         resource_type="checkin", resource_id=checkin_id,
                         detail={"session_id": session_id, "delegate_id": delegate_id},
                         occurred_at=self._moment())
            return self._store_receipt(connection, request_id=request_id, action="check_in",
                                       payload_hash=payload_hash, resource_type="checkin",
                                       resource_id=checkin_id)

    # ---------- 截止、递补与恢复 ----------

    def sweep(self, *, actor_id: str) -> dict[str, int]:
        """推进所有进行中场次的截止、递补与结束；状态持久化，进程重启后继续生效。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            stats = self._sweep_locked(connection)
            append_event(connection, actor_id=actor_id, action="maintenance.swept",
                         resource_type="maintenance", resource_id="sweep",
                         detail=dict(stats), occurred_at=self._moment())
            return stats

    def _empty_stats(self) -> dict[str, int]:
        return {"expired_invitations": 0, "promoted_invitations": 0, "confirmed_sessions": 0,
                "completed_sessions": 0, "reheld_sessions": 0, "blocked_sessions": 0}

    def _sweep_locked(self, connection) -> dict[str, int]:
        stats = self._empty_stats()
        now = self._moment()
        rows = connection.execute(
            "SELECT session_id FROM sessions WHERE status IN ('published','confirmed') "
            "ORDER BY created_at, session_id").fetchall()
        for row in rows:
            self._reconcile_session(connection, row["session_id"], now, stats)
        return stats

    def _reconcile_session(self, connection, session_id: str, now: str, stats: dict[str, int]) -> None:
        session = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if session is None or session["status"] not in SESSION_OPEN_STATUSES:
            return
        if session["end_at"] <= now:
            self._complete_session_locked(connection, session, actor_id="system", reason="session_ended")
            stats["completed_sessions"] += 1
            return
        # 1. 响应截止的邀请过期，释放其保留席位
        overdue = connection.execute(
            "SELECT * FROM invitations WHERE session_id=? AND status='invited' "
            "AND response_due_at IS NOT NULL AND response_due_at<=?",
            (session_id, now)).fetchall()
        for invitation in overdue:
            self._transition_invitation(connection, invitation, "expired",
                                        note="response_deadline", actor_id="system")
            stats["expired_invitations"] += 1
        # 2. 保留期截止仍有多方未确认：未确认邀请过期，资源保留失效
        held = connection.execute(
            "SELECT * FROM resource_holds WHERE session_id=? AND status='held'", (session_id,)).fetchall()
        pending = self._count_invitations(connection, session_id, "invited")
        accepted = self._count_invitations(connection, session_id, "accepted")
        if (session["status"] == "published" and held and session["hold_expires_at"]
                and session["hold_expires_at"] <= now and (pending or not accepted)):
            for invitation in connection.execute(
                    "SELECT * FROM invitations WHERE session_id=? AND status='invited'",
                    (session_id,)).fetchall():
                self._transition_invitation(connection, invitation, "expired",
                                            note="hold_deadline", actor_id="system")
                stats["expired_invitations"] += 1
            for hold in held:
                connection.execute("UPDATE resource_holds SET status='expired' WHERE hold_id=?",
                                   (hold["hold_id"],))
                append_event(connection, actor_id="system", action="hold.expired",
                             resource_type="hold", resource_id=hold["hold_id"],
                             detail={"session_id": session_id, "resource_type": hold["resource_type"],
                                     "resource_id": hold["resource_id"]}, occurred_at=now)
            held = []
            pending = 0
        # 3. 按稳定位次递补候补
        self._promote_waitlist(connection, session, now, stats)
        # 4. 全部确认则落定资源，仍有待确认则保证资源保留有效
        pending = self._count_invitations(connection, session_id, "invited")
        accepted = self._count_invitations(connection, session_id, "accepted")
        if session["status"] != "published":
            return
        confirmed_holds = connection.execute(
            "SELECT * FROM resource_holds WHERE session_id=? AND status='confirmed'", (session_id,)).fetchall()
        held = connection.execute(
            "SELECT * FROM resource_holds WHERE session_id=? AND status='held'", (session_id,)).fetchall()
        if pending == 0 and accepted >= 1:
            if held:
                for hold in held:
                    connection.execute(
                        "UPDATE resource_holds SET status='confirmed', expires_at=NULL WHERE hold_id=?",
                        (hold["hold_id"],))
                    append_event(connection, actor_id="system", action="hold.confirmed",
                                 resource_type="hold", resource_id=hold["hold_id"],
                                 detail={"session_id": session_id, "resource_type": hold["resource_type"],
                                         "resource_id": hold["resource_id"]}, occurred_at=now)
            elif not confirmed_holds:
                if self._resources_available(connection, session):
                    self._create_holds(connection, session, "confirmed")
                    stats["reheld_sessions"] += 1
                else:
                    append_event(connection, actor_id="system", action="session.confirm_blocked",
                                 resource_type="session", resource_id=session_id,
                                 detail={"session_id": session_id, "reason": "resource_unavailable"},
                                 occurred_at=now)
                    stats["blocked_sessions"] += 1
                    return
            connection.execute("UPDATE sessions SET status='confirmed', hold_expires_at=NULL WHERE session_id=?",
                               (session_id,))
            append_event(connection, actor_id="system", action="session.confirmed",
                         resource_type="session", resource_id=session_id,
                         detail={"session_id": session_id}, occurred_at=now)
            stats["confirmed_sessions"] += 1
        elif pending and not held and not confirmed_holds:
            if self._resources_available(connection, session):
                self._create_holds(connection, session, "held")
                stats["reheld_sessions"] += 1
            else:
                append_event(connection, actor_id="system", action="session.rehold_blocked",
                             resource_type="session", resource_id=session_id,
                             detail={"session_id": session_id, "reason": "resource_unavailable"},
                             occurred_at=now)
                stats["blocked_sessions"] += 1

    def _promote_waitlist(self, connection, session, now: str, stats: dict[str, int]) -> None:
        room = self._load_room(connection, session["room_id"])
        while True:
            if self._seat_holders(connection, session["session_id"]) >= room["capacity"]:
                return
            candidate = connection.execute(
                "SELECT * FROM invitations WHERE session_id=? AND status='waitlisted' "
                "ORDER BY position, invited_at, invitation_id LIMIT 1",
                (session["session_id"],)).fetchone()
            if candidate is None:
                return
            delegate = self._load_delegate(connection, candidate["delegate_id"])
            failure = self._eligibility_failure(connection, session, delegate)
            if failure:
                code, detail = failure
                self._record_rejection(connection, session_id=session["session_id"],
                                       delegate_id=delegate["delegate_id"], action="promote",
                                       reason_code=code, detail=detail, actor_id="system")
                self._transition_invitation(connection, candidate, "expired",
                                            note=f"promotion_failed:{code}", actor_id="system")
                stats["expired_invitations"] += 1
                continue
            due = self._iso(self._now_dt() + timedelta(seconds=session["response_ttl_seconds"]))
            connection.execute(
                "UPDATE invitations SET status='invited', response_due_at=?, version=version+1 "
                "WHERE invitation_id=?", (due, candidate["invitation_id"]))
            append_event(connection, actor_id="system", action="invitation.promoted",
                         resource_type="invitation", resource_id=candidate["invitation_id"],
                         detail={"session_id": session["session_id"], "delegate_id": delegate["delegate_id"],
                                 "position": candidate["position"], "response_due_at": due},
                         occurred_at=now)
            stats["promoted_invitations"] += 1

    # ---------- 资格核验 ----------

    def _eligibility_failure(self, connection, session, delegate) -> tuple[str, dict[str, Any]] | None:
        """核验参会资格，返回 (原因码, 细节) 或 None。"""

        delegation = self._load_delegation(connection, delegate["delegation_id"])
        if not delegate["active"]:
            return "delegate_inactive", {"delegate_id": delegate["delegate_id"]}
        if not delegation["active"]:
            return "delegation_inactive", {"delegation_id": delegation["delegation_id"]}
        if delegate["clearance_level"] < session["sensitivity_level"]:
            return "clearance_insufficient", {"required": session["sensitivity_level"],
                                              "actual": delegate["clearance_level"]}
        windows = connection.execute(
            "SELECT * FROM delegate_availability WHERE delegate_id=?",
            (delegate["delegate_id"],)).fetchall()
        if windows and not any(w["start_at"] <= session["start_at"] and w["end_at"] >= session["end_at"]
                               for w in windows):
            return "availability_mismatch", {"session_start_at": session["start_at"],
                                             "session_end_at": session["end_at"]}
        organization_id = delegation["organization_id"]
        for participant in self._session_participants(connection, session["session_id"]):
            if participant["delegate_id"] == delegate["delegate_id"]:
                continue
            hit = self._recusal_hit(connection, "delegate", delegate["delegate_id"],
                                    participant["organization_id"])
            if hit:
                return "recusal_conflict", {"with_delegate": participant["delegate_id"],
                                            "with_organization": participant["organization_id"],
                                            "reason": hit["reason"]}
            hit = self._recusal_hit(connection, "delegate", participant["delegate_id"], organization_id)
            if hit:
                return "recusal_conflict", {"with_delegate": participant["delegate_id"],
                                            "with_organization": organization_id,
                                            "reason": hit["reason"]}
        for staff in self._session_staff(connection, session["session_id"]):
            subject_type = "interpreter" if staff["staff_type"] == "interpreter" else "actor"
            hit = self._recusal_hit(connection, subject_type, staff["staff_id"], organization_id)
            if hit:
                return "recusal_conflict", {"staff_type": staff["staff_type"],
                                            "staff_id": staff["staff_id"], "reason": hit["reason"]}
        conflict = self._exclusion_conflict(connection, session, delegate["delegate_id"])
        if conflict:
            return "exclusion_conflict", {"other_session_id": conflict["session_id"],
                                          "other_start_at": conflict["start_at"],
                                          "other_end_at": conflict["end_at"]}
        return None

    def _recusal_hit(self, connection, subject_type: str, subject_id: str, organization_id: str):
        return connection.execute(
            "SELECT * FROM recusals WHERE subject_type=? AND subject_id=? "
            "AND counterparty_organization_id=? AND active=1",
            (subject_type, subject_id, organization_id)).fetchone()

    def _exclusion_conflict(self, connection, session, delegate_id: str):
        """代表已接受的场次与本场次时间重叠或同属互斥组时冲突。"""

        rows = connection.execute(
            "SELECT s.session_id, s.start_at, s.end_at, s.exclusion_group FROM invitations i "
            "JOIN sessions s ON s.session_id=i.session_id "
            "WHERE i.delegate_id=? AND i.status='accepted' AND s.status IN ('published','confirmed') "
            "AND s.session_id != ?", (delegate_id, session["session_id"])).fetchall()
        for other in rows:
            if session["exclusion_group"] and other["exclusion_group"] == session["exclusion_group"]:
                return other
            if other["start_at"] < session["end_at"] and other["end_at"] > session["start_at"]:
                return other
        return None

    def _resource_conflict(self, connection, resource_type: str, resource_id: str, window) -> str | None:
        """检查资源在其他进行中场次的有效保留里是否已被重复预订。"""

        row = connection.execute(
            "SELECT h.session_id AS session_id FROM resource_holds h "
            "JOIN sessions s ON s.session_id=h.session_id "
            "WHERE h.resource_type=? AND h.resource_id=? AND h.status IN ('held','confirmed') "
            "AND s.session_id != ? AND s.status IN ('published','confirmed') "
            "AND s.start_at < ? AND s.end_at > ? LIMIT 1",
            (resource_type, resource_id, window["session_id"], window["end_at"], window["start_at"])).fetchone()
        return row["session_id"] if row else None

    def _resources_available(self, connection, session) -> bool:
        if self._resource_conflict(connection, "room", session["room_id"], session):
            return False
        for staff in self._session_staff(connection, session["session_id"]):
            if self._resource_conflict(connection, staff["staff_type"], staff["staff_id"], session):
                return False
        return True

    # ---------- 内部状态迁移 ----------

    def _count_invitations(self, connection, session_id: str, status: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM invitations WHERE session_id=? AND status=?",
            (session_id, status)).fetchone()["count"]

    def _seat_holders(self, connection, session_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM invitations WHERE session_id=? AND status IN ('invited','accepted')",
            (session_id,)).fetchone()["count"]

    def _next_position(self, connection, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(position), 0) AS top FROM invitations WHERE session_id=? AND position IS NOT NULL",
            (session_id,)).fetchone()
        return row["top"] + 1

    def _fresh_hold_expiry(self, session) -> str:
        return self._iso(self._now_dt() + timedelta(seconds=session["hold_ttl_seconds"]))

    def _create_holds(self, connection, session, status: str) -> None:
        now = self._moment()
        expires_at = self._fresh_hold_expiry(session) if status == "held" else None
        resources = [("room", session["room_id"], None)]
        for staff in self._session_staff(connection, session["session_id"]):
            resources.append((staff["staff_type"], staff["staff_id"], staff["language"]))
        for resource_type, resource_id, language in resources:
            hold_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO resource_holds(hold_id,session_id,resource_type,resource_id,language,status,"
                "expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (hold_id, session["session_id"], resource_type, resource_id, language, status,
                 expires_at, now))
            append_event(connection, actor_id="system", action=f"hold.{status}",
                         resource_type="hold", resource_id=hold_id,
                         detail={"session_id": session["session_id"], "resource_type": resource_type,
                                 "resource_id": resource_id, "language": language, "expires_at": expires_at},
                         occurred_at=now)
        if status == "held":
            connection.execute("UPDATE sessions SET hold_expires_at=? WHERE session_id=?",
                               (expires_at, session["session_id"]))

    def _release_holds(self, connection, session_id: str, *, reason: str, actor_id: str) -> None:
        now = self._moment()
        for hold in connection.execute(
                "SELECT * FROM resource_holds WHERE session_id=? AND status IN ('held','confirmed')",
                (session_id,)).fetchall():
            connection.execute("UPDATE resource_holds SET status='released' WHERE hold_id=?",
                               (hold["hold_id"],))
            append_event(connection, actor_id=actor_id, action="hold.released",
                         resource_type="hold", resource_id=hold["hold_id"],
                         detail={"session_id": session_id, "resource_type": hold["resource_type"],
                                 "resource_id": hold["resource_id"], "reason": reason},
                         occurred_at=now)

    def _transition_invitation(self, connection, invitation, status: str, *,
                               note: str | None, actor_id: str) -> None:
        now = self._moment()
        connection.execute(
            "UPDATE invitations SET status=?, version=version+1, "
            "responded_at=COALESCE(responded_at, ?), note=? WHERE invitation_id=?",
            (status, now, note, invitation["invitation_id"]))
        append_event(connection, actor_id=actor_id, action=f"invitation.{status}",
                     resource_type="invitation", resource_id=invitation["invitation_id"],
                     detail={"session_id": invitation["session_id"],
                             "delegate_id": invitation["delegate_id"],
                             "status": status, "note": note}, occurred_at=now)

    def _complete_session_locked(self, connection, session, *, actor_id: str, reason: str) -> None:
        now = self._moment()
        session_id = session["session_id"]
        for invitation in connection.execute(
                "SELECT * FROM invitations WHERE session_id=? AND status IN ('invited','waitlisted')",
                (session_id,)).fetchall():
            self._transition_invitation(connection, invitation, "expired",
                                        note=reason, actor_id=actor_id)
        self._revoke_session_grants(connection, session_id, reason="session_completed", actor_id=actor_id)
        self._release_holds(connection, session_id, reason=reason, actor_id=actor_id)
        connection.execute("UPDATE sessions SET status='completed', hold_expires_at=NULL WHERE session_id=?",
                           (session_id,))
        append_event(connection, actor_id=actor_id, action="session.completed",
                     resource_type="session", resource_id=session_id,
                     detail={"session_id": session_id, "reason": reason}, occurred_at=now)

    def _issue_grants(self, connection, session, invitation_id: str, invitation_version: int,
                      delegate, actor_id: str) -> None:
        now = self._moment()
        materials = connection.execute("SELECT * FROM materials WHERE session_id=?",
                                       (session["session_id"],)).fetchall()
        for material in materials:
            if material["sensitivity_level"] > delegate["clearance_level"]:
                continue
            existing = connection.execute(
                "SELECT 1 FROM material_grants WHERE material_id=? AND delegate_id=? AND status='active'",
                (material["material_id"], delegate["delegate_id"])).fetchone()
            if existing:
                continue
            basis = {"invitation_id": invitation_id, "invitation_version": invitation_version,
                     "session_version": session["version"]}
            grant_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO material_grants(grant_id,material_id,session_id,delegate_id,basis_json,status,"
                "granted_at) VALUES(?,?,?,?,?,'active',?)",
                (grant_id, material["material_id"], session["session_id"], delegate["delegate_id"],
                 canonical_json(basis), now))
            append_event(connection, actor_id=actor_id, action="grant.issued",
                         resource_type="material_grant", resource_id=grant_id,
                         detail={"session_id": session["session_id"], "material_id": material["material_id"],
                                 "delegate_id": delegate["delegate_id"], "basis": basis},
                         occurred_at=now)

    def _revoke_grants(self, connection, *, session_id: str, delegate_id: str,
                       reason: str, actor_id: str) -> None:
        now = self._moment()
        rows = connection.execute(
            "SELECT * FROM material_grants WHERE session_id=? AND delegate_id=? AND status='active'",
            (session_id, delegate_id)).fetchall()
        for grant in rows:
            connection.execute(
                "UPDATE material_grants SET status='revoked', revoked_at=?, revoke_reason=? WHERE grant_id=?",
                (now, reason, grant["grant_id"]))
            append_event(connection, actor_id=actor_id, action="grant.revoked",
                         resource_type="material_grant", resource_id=grant["grant_id"],
                         detail={"session_id": session_id, "material_id": grant["material_id"],
                                 "delegate_id": delegate_id, "reason": reason}, occurred_at=now)

    def _revoke_session_grants(self, connection, session_id: str, *, reason: str, actor_id: str) -> None:
        rows = connection.execute(
            "SELECT DISTINCT delegate_id FROM material_grants WHERE session_id=? AND status='active'",
            (session_id,)).fetchall()
        for row in rows:
            self._revoke_grants(connection, session_id=session_id, delegate_id=row["delegate_id"],
                                reason=reason, actor_id=actor_id)

    def _record_rejection(self, connection, *, session_id: str, delegate_id: str, action: str,
                          reason_code: str, detail: dict[str, Any], actor_id: str) -> str:
        rejection_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO rejections(rejection_id,session_id,delegate_id,action,reason_code,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (rejection_id, session_id, delegate_id, action, reason_code,
             canonical_json(detail), self._moment()))
        append_event(connection, actor_id=actor_id, action="invitation.rejected",
                     resource_type="session", resource_id=session_id,
                     detail={"session_id": session_id, "delegate_id": delegate_id, "action": action,
                             "reason_code": reason_code, "detail": detail},
                     occurred_at=self._moment())
        return rejection_id

    def _invitation_actor_allowed(self, actor: Actor, organization_id: str, delegate) -> bool:
        if actor.role in ("admin", "operator"):
            return True
        if actor.role == "liaison" and actor.organization_id == organization_id:
            return True
        return actor.role == "delegate" and delegate["actor_id"] == actor.actor_id

    # ---------- 角色化视图与反查 ----------

    def _session_basics(self, session, site) -> dict[str, Any]:
        basics: dict[str, Any] = {
            "session_id": session["session_id"], "site_id": session["site_id"], "topic": session["topic"],
            "sensitivity_level": session["sensitivity_level"], "status": session["status"],
            "version": session["version"], "start_at": session["start_at"], "end_at": session["end_at"],
            "exclusion_group": session["exclusion_group"],
            "required_languages": json.loads(session["required_languages_json"]),
            "hold_expires_at": session["hold_expires_at"], "timezone": site["timezone_name"],
        }
        try:
            zone = ZoneInfo(site["timezone_name"])
            basics["local_start_at"] = datetime.fromisoformat(session["start_at"]).astimezone(zone).isoformat()
            basics["local_end_at"] = datetime.fromisoformat(session["end_at"]).astimezone(zone).isoformat()
        except Exception:
            pass
        return basics

    def _participant_views(self, connection, session_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT i.*, d.display_name, d.delegation_id, g.organization_id FROM invitations i "
            "JOIN delegates d ON d.delegate_id=i.delegate_id "
            "JOIN delegations g ON g.delegation_id=d.delegation_id "
            "WHERE i.session_id=? ORDER BY i.invited_at, i.invitation_id", (session_id,)).fetchall()
        return [{"invitation_id": row["invitation_id"], "delegate_id": row["delegate_id"],
                 "display_name": row["display_name"], "delegation_id": row["delegation_id"],
                 "organization_id": row["organization_id"], "status": row["status"],
                 "position": row["position"], "response_due_at": row["response_due_at"],
                 "version": row["version"], "supersedes": row["supersedes"]} for row in rows]

    def get_session_view(self, *, actor_id: str, session_id: str) -> dict[str, Any]:
        """按角色返回履职所需的场次视图。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        session = self._load_session(connection, session_id)
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (session["site_id"],)).fetchone()
        view = self._session_basics(session, site)
        room = self._load_room(connection, session["room_id"])
        if actor.role in ("admin", "operator", "compliance", "auditor", "reviewer"):
            view["room"] = {"room_id": room["room_id"], "name": room["name"],
                            "capacity": room["capacity"], "secure": bool(room["secure"])}
            view["participants"] = self._participant_views(connection, session_id)
            view["staff"] = [dict(row) for row in self._session_staff(connection, session_id)]
            view["resources"] = [dict(row) for row in connection.execute(
                "SELECT * FROM resource_holds WHERE session_id=? ORDER BY created_at, hold_id",
                (session_id,)).fetchall()]
            view["materials"] = [dict(row) for row in connection.execute(
                "SELECT material_id, title, sensitivity_level, version FROM materials WHERE session_id=? "
                "ORDER BY created_at, material_id", (session_id,)).fetchall()]
            return view
        view["room"] = {"room_id": room["room_id"], "name": room["name"]}
        if actor.role == "liaison":
            rows = connection.execute(
                "SELECT i.*, d.display_name, d.delegate_id FROM invitations i "
                "JOIN delegates d ON d.delegate_id=i.delegate_id "
                "JOIN delegations g ON g.delegation_id=d.delegation_id "
                "WHERE i.session_id=? AND g.organization_id=? ORDER BY i.invited_at, i.invitation_id",
                (session_id, actor.organization_id)).fetchall()
            if not rows:
                raise NotFoundError("场次不存在或不可见")
            view["own_invitations"] = [
                {"invitation_id": row["invitation_id"], "delegate_id": row["delegate_id"],
                 "display_name": row["display_name"], "status": row["status"],
                 "position": row["position"], "response_due_at": row["response_due_at"]}
                for row in rows]
            return view
        if actor.role == "delegate":
            delegate = connection.execute("SELECT * FROM delegates WHERE actor_id=?",
                                          (actor_id,)).fetchone()
            invitation = None
            if delegate is not None:
                invitation = connection.execute(
                    "SELECT * FROM invitations WHERE session_id=? AND delegate_id=? "
                    "ORDER BY invited_at DESC, invitation_id DESC LIMIT 1",
                    (session_id, delegate["delegate_id"])).fetchone()
            if invitation is None:
                raise NotFoundError("场次不存在或不可见")
            view["own_invitation"] = {"invitation_id": invitation["invitation_id"],
                                      "status": invitation["status"],
                                      "response_due_at": invitation["response_due_at"]}
            view["materials"] = [dict(row) for row in connection.execute(
                "SELECT m.material_id, m.title, m.sensitivity_level FROM materials m "
                "JOIN material_grants gr ON gr.material_id=m.material_id "
                "WHERE m.session_id=? AND gr.delegate_id=? AND gr.status='active' "
                "ORDER BY m.created_at, m.material_id",
                (session_id, delegate["delegate_id"])).fetchall()]
            return view
        if actor.role == "interpreter":
            interpreter = connection.execute("SELECT * FROM interpreters WHERE actor_id=?",
                                             (actor_id,)).fetchone()
            assignment = None
            if interpreter is not None:
                assignment = connection.execute(
                    "SELECT * FROM session_staff WHERE session_id=? AND staff_type='interpreter' AND staff_id=?",
                    (session_id, interpreter["interpreter_id"])).fetchone()
            if assignment is None:
                raise NotFoundError("场次不存在或不可见")
            view["assignment"] = {"interpreter_id": interpreter["interpreter_id"],
                                  "language": assignment["language"]}
            return view
        raise PermissionDenied("当前角色不能查看场次")

    def trace_session(self, *, actor_id: str, session_id: str) -> dict[str, Any]:
        """从场次反查参与者、资源、保密依据与历次变更。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "compliance", "auditor")
        session = self._load_session(connection, session_id)
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (session["site_id"],)).fetchone()
        materials = []
        for material in connection.execute(
                "SELECT * FROM materials WHERE session_id=? ORDER BY created_at, material_id",
                (session_id,)).fetchall():
            grants = [dict(row) for row in connection.execute(
                "SELECT * FROM material_grants WHERE material_id=? ORDER BY rowid",
                (material["material_id"],)).fetchall()]
            for grant in grants:
                grant["basis"] = json.loads(grant.pop("basis_json"))
            accesses = [dict(row) for row in connection.execute(
                "SELECT * FROM material_accesses WHERE material_id=? ORDER BY accessed_at, access_id",
                (material["material_id"],)).fetchall()]
            materials.append({"material_id": material["material_id"], "title": material["title"],
                              "sensitivity_level": material["sensitivity_level"],
                              "version": material["version"], "grants": grants, "accesses": accesses})
        changes = []
        for row in connection.execute(
                "SELECT * FROM audit_events WHERE resource_id=? OR json_extract(detail_json, '$.session_id')=? "
                "ORDER BY sequence", (session_id, session_id)).fetchall():
            changes.append({"sequence": row["sequence"], "event_id": row["event_id"],
                            "actor_id": row["actor_id"], "action": row["action"],
                            "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                            "detail": json.loads(row["detail_json"]), "event_hash": row["event_hash"],
                            "occurred_at": row["occurred_at"]})
        return {
            "session": self._session_basics(session, site),
            "participants": self._participant_views(connection, session_id),
            "staff": [dict(row) for row in self._session_staff(connection, session_id)],
            "resources": [dict(row) for row in connection.execute(
                "SELECT * FROM resource_holds WHERE session_id=? ORDER BY created_at, hold_id",
                (session_id,)).fetchall()],
            "materials": materials,
            "checkins": [dict(row) for row in connection.execute(
                "SELECT * FROM checkins WHERE session_id=? ORDER BY checked_in_at, checkin_id",
                (session_id,)).fetchall()],
            "rejections": self._rejection_rows(connection, session_id=session_id),
            "changes": changes,
        }

    def _rejection_rows(self, connection, *, session_id: str | None = None,
                        delegate_id: str | None = None,
                        organization_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT r.* FROM rejections r JOIN delegates d ON d.delegate_id=r.delegate_id "
                 "JOIN delegations g ON g.delegation_id=d.delegation_id WHERE 1=1")
        parameters: list[Any] = []
        if session_id:
            query += " AND r.session_id=?"
            parameters.append(session_id)
        if delegate_id:
            query += " AND r.delegate_id=?"
            parameters.append(delegate_id)
        if organization_id:
            query += " AND g.organization_id=?"
            parameters.append(organization_id)
        query += " ORDER BY r.created_at, r.rejection_id"
        return [{"rejection_id": row["rejection_id"], "session_id": row["session_id"],
                 "delegate_id": row["delegate_id"], "action": row["action"],
                 "reason_code": row["reason_code"], "detail": json.loads(row["detail_json"]),
                 "created_at": row["created_at"]}
                for row in connection.execute(query, parameters).fetchall()]

    def list_rejections(self, *, actor_id: str, session_id: str | None = None,
                        delegate_id: str | None = None) -> list[dict[str, Any]]:
        """按角色返回拒绝记录，用于向被拒代表解释具体冲突。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if actor.role in ("admin", "operator", "compliance", "auditor"):
            return self._rejection_rows(connection, session_id=session_id, delegate_id=delegate_id)
        if actor.role == "liaison":
            return self._rejection_rows(connection, session_id=session_id, delegate_id=delegate_id,
                                        organization_id=actor.organization_id)
        if actor.role == "delegate":
            delegate = connection.execute("SELECT * FROM delegates WHERE actor_id=?",
                                          (actor_id,)).fetchone()
            if delegate is None:
                raise PermissionDenied("当前角色不能查询拒绝记录")
            if delegate_id and delegate_id != delegate["delegate_id"]:
                raise PermissionDenied("只能查询本人的拒绝记录")
            return self._rejection_rows(connection, session_id=session_id,
                                        delegate_id=delegate["delegate_id"])
        raise PermissionDenied("当前角色不能查询拒绝记录")

    def my_invitations(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        base = ("SELECT i.*, s.topic, s.start_at, s.end_at, s.status AS session_status, "
                "d.display_name FROM invitations i "
                "JOIN sessions s ON s.session_id=i.session_id "
                "JOIN delegates d ON d.delegate_id=i.delegate_id ")
        parameters: list[Any] = []
        if actor.role == "delegate":
            delegate = connection.execute("SELECT * FROM delegates WHERE actor_id=?",
                                          (actor_id,)).fetchone()
            if delegate is None:
                raise PermissionDenied("当前角色没有邀请视图")
            query = base + "WHERE i.delegate_id=? "
            parameters.append(delegate["delegate_id"])
        elif actor.role == "liaison":
            query = (base + "JOIN delegations g ON g.delegation_id=d.delegation_id "
                     "WHERE g.organization_id=? ")
            parameters.append(actor.organization_id)
        elif actor.role in ("admin", "operator", "compliance", "auditor"):
            query = base + "WHERE 1=1 "
        else:
            raise PermissionDenied("当前角色没有邀请视图")
        query += "ORDER BY i.invited_at, i.invitation_id"
        return [{"invitation_id": row["invitation_id"], "session_id": row["session_id"],
                 "topic": row["topic"], "start_at": row["start_at"], "end_at": row["end_at"],
                 "session_status": row["session_status"], "delegate_id": row["delegate_id"],
                 "display_name": row["display_name"], "status": row["status"],
                 "position": row["position"], "response_due_at": row["response_due_at"],
                 "version": row["version"]}
                for row in connection.execute(query, parameters).fetchall()]

    def my_assignments(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if actor.role == "interpreter":
            interpreter = connection.execute("SELECT * FROM interpreters WHERE actor_id=?",
                                             (actor_id,)).fetchone()
            if interpreter is None:
                raise PermissionDenied("当前角色没有分派视图")
            staff_type, staff_id = "interpreter", interpreter["interpreter_id"]
        elif actor.role == "compliance":
            staff_type, staff_id = "observer", actor.actor_id
        else:
            raise PermissionDenied("当前角色没有分派视图")
        rows = connection.execute(
            "SELECT st.*, s.topic, s.start_at, s.end_at, s.status AS session_status, "
            "s.sensitivity_level FROM session_staff st JOIN sessions s ON s.session_id=st.session_id "
            "WHERE st.staff_type=? AND st.staff_id=? ORDER BY s.start_at, st.session_id",
            (staff_type, staff_id)).fetchall()
        return [{"session_id": row["session_id"], "topic": row["topic"], "start_at": row["start_at"],
                 "end_at": row["end_at"], "session_status": row["session_status"],
                 "sensitivity_level": row["sensitivity_level"], "staff_type": row["staff_type"],
                 "language": row["language"]} for row in rows]
