"""会谈编排服务的离线端到端验收。

复现数贸会开幕场景：同一家企业被多场名单重复引用、资源重复预订、
临时替换代表仍能下载材料、候补保留期限、进程恢复递补、冲突解释与反查。
成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import PermissionDenied
from .scheduling import SchedulingService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scheduling_acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        base = DomainService(database, clock)
        service = SchedulingService(database, clock, hold_minutes=60)

        # ---- 基础：两个机构、操作者与联络人 ----
        base.register_organization(request_id="org-a", actor_id="bootstrap",
                                   organization_id="org-a", name="甲国数字贸易署")
        base.register_organization(request_id="org-b", actor_id="bootstrap",
                                   organization_id="org-b", name="环球采购集团")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                            display_name="活动总指挥", role="admin", organization_id="org-a")
        base.register_actor(request_id="liaison-a", actor_id="admin", new_actor_id="liaison-a",
                            display_name="国家馆联络人甲", role="liaison", organization_id="org-a")
        base.register_actor(request_id="liaison-b", actor_id="admin", new_actor_id="liaison-b",
                            display_name="企业联络员乙", role="liaison", organization_id="org-b")

        # ---- 代表团与代表（以代表团-机构关联为资格基础）----
        service.register_delegation(request_id="del-a", actor_id="admin", delegation_id="del-a",
                                    organization_id="org-a", name="甲国国家馆代表团",
                                    liaison_actor_id="liaison-a")
        service.register_delegation(request_id="del-b", actor_id="admin", delegation_id="del-b",
                                    organization_id="org-b", name="环球采购集团代表团",
                                    liaison_actor_id="liaison-b")
        service.register_representative(request_id="p-min", actor_id="admin",
                                        representative_id="p-min", delegation_id="del-a",
                                        organization_id="org-a", display_name="甲国部长",
                                        clearance="secret")
        service.register_representative(request_id="p-vice", actor_id="admin",
                                        representative_id="p-vice", delegation_id="del-a",
                                        organization_id="org-a", display_name="甲国副手",
                                        clearance="secret")
        for rid, name, clr in [("p-co", "环球公司首席代表", "controlled"),
                               ("p-dd", "环球公司尽调专员", "confidential"),
                               ("p-co2", "环球公司采购副手", "controlled"),
                               ("p-backup", "环球公司候补", "confidential"),
                               ("p-replaced", "被临时替换的代表", "open")]:
            service.register_representative(request_id=f"rep-{rid}", actor_id="admin",
                                            representative_id=rid, delegation_id="del-b",
                                            organization_id="org-b", display_name=name,
                                            clearance=clr)

        # ---- 保障资源 ----
        service.register_resource(request_id="room-sc", actor_id="admin", resource_id="room-sc",
                                  kind="room", label="保密会议室", capacity=6, sensitivity="secret")
        service.register_resource(request_id="room-2", actor_id="admin", resource_id="room-2",
                                  kind="room", label="二号会议室", capacity=10, sensitivity="controlled")
        service.register_resource(request_id="interp", actor_id="admin", resource_id="interp",
                                  kind="interpreter", label="中英同传", sensitivity="secret",
                                  languages=[{"language_code": "zh", "level": "native"},
                                             {"language_code": "en", "level": "fluent"}])
        service.register_resource(request_id="observer", actor_id="admin", resource_id="observer",
                                  kind="observer", label="合规观察员", capacity=2, sensitivity="secret")

        # ---- 三场会谈 ----
        service.create_session(request_id="s-minister", actor_id="admin", session_id="minister",
                               title="部长闭门会", session_type="minister_closed",
                               sensitivity="secret", starts_at="2026-11-05T09:00:00Z",
                               ends_at="2026-11-05T10:00:00Z", timezone_name="Asia/Shanghai",
                               venue_id="room-sc", capacity=2, language_codes=["zh", "en"],
                               resource_ids=["interp", "observer"])
        service.create_session(request_id="s-proc", actor_id="admin", session_id="procure",
                               title="采购洽谈", session_type="procurement",
                               sensitivity="controlled", starts_at="2026-11-05T10:30:00Z",
                               ends_at="2026-11-05T11:30:00Z", timezone_name="Asia/Shanghai",
                               venue_id="room-2", capacity=1, language_codes=["zh", "en"],
                               resource_ids=["interp"])
        service.create_session(request_id="s-dd", actor_id="admin", session_id="dd",
                               title="技术尽调", session_type="technical_dd",
                               sensitivity="confidential", starts_at="2026-11-05T13:00:00Z",
                               ends_at="2026-11-05T14:00:00Z", timezone_name="Asia/Shanghai",
                               venue_id="room-sc", capacity=4, language_codes=["zh"],
                               resource_ids=["interp"])
        # 同一家企业不能同时进采购与尽调（互斥场次）
        service.add_exclusion(request_id="ex-proc-dd", actor_id="admin",
                              session_id_a="dd", session_id_b="procure")

        # ---- 资源重复预订：闭门会进行中再订同一保密会议室必须被拒 ----
        double_book_blocked = False
        try:
            service.create_session(request_id="s-clash", actor_id="admin", session_id="clash",
                                   title="重复占用", session_type="bilateral",
                                   sensitivity="secret", starts_at="2026-11-05T09:30:00Z",
                                   ends_at="2026-11-05T10:30:00Z",
                                   timezone_name="Asia/Shanghai", venue_id="room-sc",
                                   capacity=2, language_codes=["zh", "en"],
                                   resource_ids=["interp", "observer"])
        except Exception:
            double_book_blocked = True

        # ---- 名单冲突：p-co 已确认采购，再进尽调被互斥拒绝并可解释 ----
        proc_invite = service.invite(request_id="i-co-proc", actor_id="admin",
                                     session_id="procure", representative_id="p-co")
        service.respond_invitation(request_id="acc-co", actor_id="liaison-b",
                                   seat_id=proc_invite["seat_id"], accept=True)
        dd_rejection = service.invite(request_id="i-co-dd", actor_id="admin",
                                      session_id="dd", representative_id="p-co")
        rejection_explained = dd_rejection["decision"] == "rejected" and \
            any(c["code"] == "exclusive_session" for c in dd_rejection["conflicts"])
        decision_detail = service.get_decision(dd_rejection["decision_id"], actor_id="liaison-b")
        exclusive_conflict = next(c for c in decision_detail["conflicts"]
                                  if c["code"] == "exclusive_session")

        # ---- 知悉级别不足：open 级别代表进 confidential 尽调被拒 ----
        low_clearance = service.invite(request_id="i-replaced", actor_id="admin",
                                       session_id="dd", representative_id="p-replaced")
        low_clearance_blocked = low_clearance["decision"] == "rejected" and \
            any(c["code"] == "clearance_insufficient" for c in low_clearance["conflicts"])

        # ---- 尽调名单：尽调专员确认，候补进入 ----
        dd_invite = service.invite(request_id="i-dd-dd", actor_id="admin",
                                   session_id="dd", representative_id="p-dd")
        service.respond_invitation(request_id="acc-dd", actor_id="liaison-b",
                                   seat_id=dd_invite["seat_id"], accept=True)
        backup = service.invite(request_id="i-backup", actor_id="admin",
                                session_id="dd", representative_id="p-backup")
        service.respond_invitation(request_id="acc-backup", actor_id="liaison-b",
                                   seat_id=backup["seat_id"], accept=True)

        # ---- 部长闭门会：部长确认，材料随席位开放，转授权后原代表立即失去权限 ----
        min_invite = service.invite(request_id="i-min", actor_id="admin",
                                    session_id="minister", representative_id="p-min")
        service.respond_invitation(request_id="acc-min", actor_id="liaison-a",
                                   seat_id=min_invite["seat_id"], accept=True)
        service.upload_material(request_id="mat-min", actor_id="admin", session_id="minister",
                                material_id="briefing", title="闭门会秘密简报",
                                sensitivity="secret", content_hash="sha256:demo")
        before_delegate = service.access_material(actor_id="admin", material_id="briefing",
                                                  representative_id="p-min")["allowed"]
        delegation = service.delegate(request_id="dlg-min", actor_id="liaison-a",
                                      seat_id=min_invite["seat_id"],
                                      to_representative_id="p-vice", reason="部长临时另有双边")
        try:
            service.access_material(actor_id="admin", material_id="briefing",
                                    representative_id="p-min")
            lost_after_delegate = False
        except PermissionDenied:
            lost_after_delegate = True
        vice_access = service.access_material(actor_id="admin", material_id="briefing",
                                              representative_id="p-vice")
        # 副手已产生访问事实：此时撤销转授权恢复部长席位必须被拒绝，事实不可覆盖。
        revocation_blocked = False
        try:
            service.revoke_delegation(request_id="rev-min", actor_id="liaison-a",
                                      authorization_id=delegation["authorization_id"])
        except Exception:
            revocation_blocked = True

        # ---- 采购会只有 1 席：企业采购副手进入稳定候补 ----
        waitlisted = service.invite(request_id="i-co2-proc", actor_id="admin",
                                    session_id="procure", representative_id="p-co2")
        waitlist_rank = waitlisted.get("waitlist_rank")

        # ---- 进程恢复：关闭并重新打开数据库，时钟推进数日，候补位置与期限延续 ----
        valid_before, events_before = service.verify_audit()
        database.close()
        clock2 = FixedClock(datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc))
        database = Database(path)
        base = DomainService(database, clock2)
        service = SchedulingService(database, clock2, hold_minutes=60)
        withdraw = service.withdraw(request_id="wd-co", actor_id="liaison-b",
                                    seat_id=proc_invite["seat_id"], reason="行程调整")
        promoted_rep = withdraw["promotions"][0]["representative_id"] if withdraw["promotions"] else None
        valid_after, events_after = service.verify_audit()

        # ---- 反查：任一场次可回溯参与者、资源、保密依据与历次变更 ----
        view = service.session_view("minister", actor_id="liaison-a")
        reverse_lookup_ok = any(p["representative_id"] == "p-vice" for p in view.participants) and \
            any(r["resource_id"] == "room-sc" for r in view.resources) and \
            len(view.history) >= 3

        result = {
            "status": "ok",
            "double_book_blocked": double_book_blocked,
            "rejection_explained": rejection_explained,
            "rejection_competing_session":
                exclusive_conflict.get("competing_session_id"),
            "low_clearance_blocked": low_clearance_blocked,
            "material_open_before_delegate": before_delegate,
            "material_revoked_after_delegate": lost_after_delegate,
            "delegate_access_allowed": vice_access["allowed"],
            "delegate_revocation_blocked_by_fact": revocation_blocked,
            "waitlist_rank": waitlist_rank,
            "promoted_after_withdraw": promoted_rep,
            "restart_preserved_audit": valid_after and events_after > events_before,
            "reverse_lookup_ok": reverse_lookup_ok,
            "audit_valid": valid_after,
            "audit_events": events_after,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ("double_book_blocked", "rejection_explained", "low_clearance_blocked",
                "material_open_before_delegate", "material_revoked_after_delegate",
                "delegate_access_allowed", "delegate_revocation_blocked_by_fact",
                "restart_preserved_audit", "reverse_lookup_ok", "audit_valid")
    ok = result["status"] == "ok" and all(result[key] for key in required) and \
        result["promoted_after_withdraw"] == "p-co2" and result["waitlist_rank"] == 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
