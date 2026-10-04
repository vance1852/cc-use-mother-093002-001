"""会谈编排测试共用的登记夹具。"""

from __future__ import annotations

from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.scheduling import SchedulingService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database


def build_fixture(start: datetime | None = None, hold_minutes: int = 60, path: str | None = None):
    """建立包含两个机构、两个代表团、代表、资源的标准夹具。

    传入 path 时使用文件数据库；重复以同一 path 构建时，登记动作按 request_id 幂等重放。
    """

    database = Database(path) if path else Database()
    start = start or datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = FixedClock(start)
    base = DomainService(database, clock)
    service = SchedulingService(database, clock, hold_minutes=hold_minutes)

    # 重开已有数据库时基础登记都已完成，跳过即可（状态全部持久化）。
    already_seeded = database.connection.execute(
        "SELECT COUNT(*) AS c FROM organizations").fetchone()["c"] > 0
    if already_seeded:
        return database, clock, base, service

    base.register_organization(request_id="org-a", actor_id="bootstrap",
                               organization_id="org-a", name="甲国数字贸易署")
    base.register_organization(request_id="org-b", actor_id="bootstrap",
                               organization_id="org-b", name="乙国采购联盟")
    base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                        display_name="总指挥", role="admin", organization_id="org-a")
    base.register_actor(request_id="op", actor_id="admin", new_actor_id="op1",
                        display_name="操作员", role="operator", organization_id="org-a")
    base.register_actor(request_id="liaison-a", actor_id="admin", new_actor_id="liaison-a",
                        display_name="甲国联络人", role="liaison", organization_id="org-a")
    base.register_actor(request_id="liaison-b", actor_id="admin", new_actor_id="liaison-b",
                        display_name="乙国联络人", role="liaison", organization_id="org-b")
    base.register_actor(request_id="rmgr", actor_id="admin", new_actor_id="rmgr1",
                        display_name="资源管理员", role="resource_manager", organization_id="org-a")
    base.register_actor(request_id="observer", actor_id="admin", new_actor_id="obs1",
                        display_name="现场观察员", role="observer", organization_id="org-a")

    service.register_delegation(request_id="del-a", actor_id="admin", delegation_id="del-a",
                                organization_id="org-a", name="甲国代表团",
                                liaison_actor_id="liaison-a")
    service.register_delegation(request_id="del-b", actor_id="admin", delegation_id="del-b",
                                organization_id="org-b", name="乙国代表团",
                                liaison_actor_id="liaison-b")

    representatives = [
        ("p-a1", "del-a", "org-a", "甲国代表一", "secret"),
        ("p-a2", "del-a", "org-a", "甲国代表二", "secret"),
        ("p-a3", "del-a", "org-a", "甲国代表三", "secret"),
        ("p-b1", "del-b", "org-b", "乙国代表一", "controlled"),
        ("p-b2", "del-b", "org-b", "乙国代表二", "confidential"),
        ("p-b3", "del-b", "org-b", "乙国代表三", "open"),
    ]
    for rid, did, oid, name, clearance in representatives:
        service.register_representative(request_id=f"rep-{rid}", actor_id="admin",
                                        representative_id=rid, delegation_id=did,
                                        organization_id=oid, display_name=name, clearance=clearance)

    service.register_resource(request_id="room-secret", actor_id="admin", resource_id="room-secret",
                              kind="room", label="保密会议室", capacity=4, sensitivity="secret")
    service.register_resource(request_id="room-open", actor_id="admin", resource_id="room-open",
                              kind="room", label="普通会议室", capacity=8, sensitivity="open")
    service.register_resource(request_id="room-ctrl", actor_id="admin", resource_id="room-ctrl",
                              kind="room", label="受控会议室", capacity=8, sensitivity="controlled")
    service.register_resource(request_id="interp", actor_id="admin", resource_id="interp",
                              kind="interpreter", label="中英译员", sensitivity="secret",
                              languages=[{"language_code": "zh", "level": "native"},
                                         {"language_code": "en", "level": "fluent"}])
    service.register_resource(request_id="interp-en", actor_id="admin", resource_id="interp-en",
                              kind="interpreter", label="英语译员", sensitivity="controlled",
                              languages=[{"language_code": "en", "level": "native"}])
    service.register_resource(request_id="observer-res", actor_id="admin", resource_id="observer-res",
                              kind="observer", label="合规观察员", capacity=1, sensitivity="secret")

    return database, clock, base, service


def make_session(service, session_id="s1", **overrides):
    """默认创建一场 11 月 5 日 09:00–10:00 UTC 的秘密闭门会。"""

    params = {
        "request_id": f"req-{session_id}", "actor_id": "admin", "session_id": session_id,
        "title": "闭门会", "session_type": "minister_closed", "sensitivity": "secret",
        "starts_at": "2026-11-05T09:00:00Z", "ends_at": "2026-11-05T10:00:00Z",
        "timezone_name": "Asia/Shanghai", "venue_id": "room-secret", "capacity": 2,
        "language_codes": ["zh", "en"], "resource_ids": ["interp", "observer-res"],
    }
    params.update(overrides)
    return service.create_session(**params)
