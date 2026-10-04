"""运行会谈编排服务的离线端到端验收。

场景对应数贸会开幕日：部长闭门会的代表团资格核验、翻译与保密会议室和
合规观察员的防重复预订、邀请候补与截止递补、转授权与临时替换后的材料
权限收回、签到与访问事实保留，以及进程重启后候补位次与到期时间的延续。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import ManualClock
from .scheduling import MeetingService
from .storage import Database


WINDOW = [{"start_at": "2026-10-04T00:00:00+08:00", "end_at": "2026-10-06T00:00:00+08:00"}]


def _setup_world(service: MeetingService) -> None:
    service.register_organization(request_id="acc-org-host", actor_id="bootstrap",
                                  organization_id="org-host", name="数贸会主办方")
    service.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="系统管理员", role="admin", organization_id="org-host")
    service.register_actor(request_id="acc-op", actor_id="admin", new_actor_id="operator",
                           display_name="国家馆联络组", role="operator", organization_id="org-host")
    service.register_actor(request_id="acc-obs", actor_id="admin", new_actor_id="observer",
                           display_name="合规观察员", role="compliance", organization_id="org-host")
    service.register_actor(request_id="acc-aud", actor_id="admin", new_actor_id="auditor",
                           display_name="审计员", role="auditor", organization_id="org-host")
    service.register_organization(request_id="acc-org-a", actor_id="admin",
                                  organization_id="org-a", name="甲国国家馆")
    service.register_organization(request_id="acc-org-b", actor_id="admin",
                                  organization_id="org-b", name="乙跨国企业")
    service.register_actor(request_id="acc-liaison-a", actor_id="admin", new_actor_id="liaison-a",
                           display_name="甲国联络员", role="liaison", organization_id="org-a")
    service.register_actor(request_id="acc-actor-a1", actor_id="admin", new_actor_id="actor-a1",
                           display_name="甲一账号", role="delegate", organization_id="org-a")
    service.register_actor(request_id="acc-actor-a3", actor_id="admin", new_actor_id="actor-a3",
                           display_name="甲三账号", role="delegate", organization_id="org-a")
    service.register_site(request_id="acc-site", actor_id="operator", site_id="site-1",
                          organization_id="org-host", name="国家会展中心",
                          timezone_name="Asia/Shanghai")
    service.register_room(request_id="acc-room", actor_id="operator", room_id="room-vip",
                          site_id="site-1", name="保密会议室一", capacity=2, secure=True)
    service.register_delegation(request_id="acc-del-a", actor_id="operator", delegation_id="del-a",
                                organization_id="org-a", name="甲国代表团")
    service.register_delegation(request_id="acc-del-b", actor_id="operator", delegation_id="del-b",
                                organization_id="org-b", name="乙企业代表团")
    service.register_delegate(request_id="acc-d-a1", actor_id="liaison-a", delegate_id="d-a1",
                              delegation_id="del-a", display_name="甲一", clearance_level=4,
                              languages=["zh", "en"], actor_link="actor-a1", availability=WINDOW)
    service.register_delegate(request_id="acc-d-a2", actor_id="liaison-a", delegate_id="d-a2",
                              delegation_id="del-a", display_name="甲二", clearance_level=2,
                              languages=["zh"], availability=WINDOW)
    service.register_delegate(request_id="acc-d-a3", actor_id="liaison-a", delegate_id="d-a3",
                              delegation_id="del-a", display_name="甲三", clearance_level=4,
                              languages=["zh", "fr"], actor_link="actor-a3", availability=WINDOW)
    service.register_delegate(request_id="acc-d-b1", actor_id="operator", delegate_id="d-b1",
                              delegation_id="del-b", display_name="乙一", clearance_level=3,
                              languages=["en"], availability=WINDOW)
    service.register_delegate(request_id="acc-d-b2", actor_id="operator", delegate_id="d-b2",
                              delegation_id="del-b", display_name="乙二", clearance_level=4,
                              languages=["en", "zh"], availability=WINDOW)
    service.register_interpreter(request_id="acc-it", actor_id="operator", interpreter_id="it-1",
                                 site_id="site-1", display_name="中英高翻", languages=["zh", "en"],
                                 clearance_level=4)
    # 乙一与甲国机构存在回避关系，不能同场
    service.register_recusal(request_id="acc-rec", actor_id="observer", subject_type="delegate",
                             subject_id="d-b1", counterparty_organization_id="org-a",
                             reason="乙一正接受甲国反垄断问询")


def _create_minister_session(service: MeetingService) -> None:
    service.create_session(request_id="acc-cs", actor_id="operator", session_id="minister-1",
                           site_id="site-1", topic="部长闭门会", sensitivity_level=3,
                           start_at="2026-10-04T09:00:00+08:00",
                           end_at="2026-10-04T11:00:00+08:00", room_id="room-vip",
                           required_languages=["zh"], response_ttl_seconds=1800,
                           hold_ttl_seconds=3600)
    service.assign_interpreter(request_id="acc-ai", actor_id="operator", session_id="minister-1",
                               interpreter_id="it-1", language="zh")
    service.assign_observer(request_id="acc-ao", actor_id="operator", session_id="minister-1",
                            observer_actor_id="observer")
    service.publish_session(request_id="acc-pub", actor_id="operator", session_id="minister-1")


def run() -> dict[str, object]:
    """执行完整验收链并返回结果。"""

    checks: list[str] = []

    def expect(condition: bool, label: str) -> None:
        if not condition:
            raise AssertionError(f"验收失败：{label}")
        checks.append(label)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        clock = ManualClock(datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc))  # 北京 08:00
        database = Database(path)
        service = MeetingService(database, clock)
        _setup_world(service)
        _create_minister_session(service)

        # 资格核验：回避关系与密级不足被拒并留痕
        service.invite_delegate(request_id="acc-inv-a1", actor_id="liaison-a",
                                session_id="minister-1", delegate_id="d-a1")
        try:
            service.invite_delegate(request_id="acc-inv-b1", actor_id="operator",
                                    session_id="minister-1", delegate_id="d-b1")
            raise AssertionError("回避关系代表不应被邀请")
        except Exception as exc:
            expect("recusal_conflict" in str(exc), "回避冲突被拒")
        try:
            service.invite_delegate(request_id="acc-inv-a2", actor_id="liaison-a",
                                    session_id="minister-1", delegate_id="d-a2")
            raise AssertionError("密级不足代表不应被邀请")
        except Exception as exc:
            expect("clearance_insufficient" in str(exc), "密级不足被拒")
        rejections = service.list_rejections(actor_id="liaison-a", session_id="minister-1")
        expect([r["reason_code"] for r in rejections] == ["clearance_insufficient"],
               "联络员只见本机构拒绝原因")
        all_rejections = service.list_rejections(actor_id="operator", session_id="minister-1")
        expect({r["reason_code"] for r in all_rejections} == {"recusal_conflict", "clearance_insufficient"},
               "联络组可解释全部拒绝原因")

        # 容量 2：乙二接受后甲三候补，甲一未在截止前确认被递补
        second = service.invite_delegate(request_id="acc-inv-b2", actor_id="operator",
                                         session_id="minister-1", delegate_id="d-b2")
        service.invite_delegate(request_id="acc-inv-a3", actor_id="liaison-a",
                                session_id="minister-1", delegate_id="d-a3")
        view = service.get_session_view(actor_id="liaison-a", session_id="minister-1")
        own = {row["delegate_id"]: row["status"] for row in view["own_invitations"]}
        expect(own.get("d-a3") == "waitlisted", "甲三进入候补")
        service.respond_invitation(request_id="acc-acc-b2", actor_id="operator",
                                   invitation_id=second.resource_id, decision="accept")
        clock.advance(seconds=1801)  # 越过甲一响应截止
        stats = service.sweep(actor_id="operator")
        expect(stats["expired_invitations"] == 1 and stats["promoted_invitations"] == 1,
               "截止后按位次递补")

        # 进程重启：候补位次与到期时间从 SQLite 恢复
        database.close()
        clock2 = ManualClock(datetime(2026, 10, 4, 0, 30, 2, tzinfo=timezone.utc))
        database = Database(path)
        service = MeetingService(database, clock2)
        invitations = service.my_invitations(actor_id="liaison-a")
        promoted = [row for row in invitations if row["delegate_id"] == "d-a3"][0]
        expect(promoted["status"] == "invited" and promoted["position"] == 1,
               "重启后候补位次延续")
        expect(promoted["response_due_at"] == "2026-10-04T01:00:01Z", "重启后到期时间延续")

        # 甲三接受，全部确认后场次落定、资源确认
        service.respond_invitation(request_id="acc-acc-a3", actor_id="actor-a3",
                                   invitation_id=promoted["invitation_id"], decision="accept")
        view = service.get_session_view(actor_id="operator", session_id="minister-1")
        expect(view["status"] == "confirmed", "全部确认后场次落定")
        expect({r["status"] for r in view["resources"]} == {"confirmed"}, "资源保留转为确认")

        # 材料授权跟随有效席位与承诺版本
        service.add_material(request_id="acc-mat", actor_id="operator", material_id="mat-brief",
                             session_id="minister-1", title="部长会底稿", sensitivity_level=3)
        service.access_material(request_id="acc-dl-a3", actor_id="actor-a3", material_id="mat-brief")

        # 转授权：甲三把席位转给同团甲一，材料权限随之转移
        invitation_a3 = promoted["invitation_id"]
        service.delegate_invitation(request_id="acc-xfer", actor_id="liaison-a",
                                    invitation_id=invitation_a3, target_delegate_id="d-a1")
        try:
            service.access_material(request_id="acc-dl-a3b", actor_id="actor-a3",
                                    material_id="mat-brief")
            raise AssertionError("转授权后原代表不应再能下载")
        except Exception as exc:
            expect("没有有效材料授权" in str(exc), "转授权后原代表权限收回")
        service.access_material(request_id="acc-dl-a1", actor_id="actor-a1", material_id="mat-brief")

        # 临时替换：甲一被替换后立即失去下载权限
        service.replace_delegate(request_id="acc-rep", actor_id="liaison-a", delegate_id="d-a1",
                                 new_delegate_id="d-a1b", display_name="甲一（接替）",
                                 clearance_level=4, languages=["zh"], availability=WINDOW)
        try:
            service.access_material(request_id="acc-dl-a1b", actor_id="actor-a1",
                                    material_id="mat-brief")
            raise AssertionError("被替换代表不应再能下载")
        except Exception as exc:
            expect("没有有效材料授权" in str(exc) or "已停用" in str(exc), "被替换代表权限收回")

        # 签到与结束：事实保留，不被新名单覆盖
        clock2.advance(minutes=31)  # 北京 08:31，签到窗口内
        service.check_in(request_id="acc-ck-b2", actor_id="operator", session_id="minister-1",
                         delegate_id="d-b2")
        clock2.advance(hours=3)  # 越过结束时间
        stats = service.sweep(actor_id="operator")
        expect(stats["completed_sessions"] == 1, "到期自动结束场次")
        trace = service.trace_session(actor_id="auditor", session_id="minister-1")
        expect(len(trace["checkins"]) == 1, "签到事实保留")
        expect(len(trace["materials"][0]["accesses"]) == 2, "访问事实保留")
        expect(trace["session"]["status"] == "completed", "场次已结束")
        actions = {change["action"] for change in trace["changes"]}
        expect({"session.published", "invitation.promoted", "invitation.accepted",
                "invitation.delegated", "invitation.revoked", "grant.issued", "grant.revoked",
                "material.accessed", "checkin.recorded", "session.completed"} <= actions,
               "历次变更可反查")
        valid, event_count = service.verify_audit()
        expect(valid, "审计链完整")
        result = {"status": "ok", "checks": checks, "audit_events": event_count,
                  "audit_valid": valid}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
