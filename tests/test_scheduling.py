import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import ManualClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.scheduling import MeetingService
from digital_trade_foundation.storage import Database


START = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)  # 北京时间 08:00
SESSION_START = "2026-10-04T09:00:00+08:00"
SESSION_END = "2026-10-04T11:00:00+08:00"
WINDOW = [{"start_at": "2026-10-04T00:00:00+08:00", "end_at": "2026-10-06T00:00:00+08:00"}]


class MeetingTestBase(unittest.TestCase):
    """搭建主办方、两个机构代表团、翻译员与合规观察员的通用环境。"""

    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(START)
        self.service = MeetingService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org-host", actor_id="bootstrap",
                                organization_id="org-host", name="主办方")
        s.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin",
                         display_name="系统管理员", role="admin", organization_id="org-host")
        s.register_actor(request_id="actor-op", actor_id="admin", new_actor_id="op",
                         display_name="联络组", role="operator", organization_id="org-host")
        s.register_actor(request_id="actor-auditor", actor_id="admin", new_actor_id="auditor",
                         display_name="审计员", role="auditor", organization_id="org-host")
        s.register_actor(request_id="actor-obs", actor_id="admin", new_actor_id="observer",
                         display_name="合规观察员", role="compliance", organization_id="org-host")
        s.register_actor(request_id="actor-obs2", actor_id="admin", new_actor_id="observer-2",
                         display_name="合规观察员二", role="compliance", organization_id="org-host")
        s.register_actor(request_id="actor-interp", actor_id="admin", new_actor_id="interp-actor",
                         display_name="翻译账号", role="interpreter", organization_id="org-host")
        s.register_organization(request_id="org-a", actor_id="admin", organization_id="org-a", name="甲国馆")
        s.register_organization(request_id="org-b", actor_id="admin", organization_id="org-b", name="乙企业")
        s.register_actor(request_id="actor-liaison-a", actor_id="admin", new_actor_id="liaison-a",
                         display_name="甲国联络员", role="liaison", organization_id="org-a")
        s.register_actor(request_id="actor-delegate-a1", actor_id="admin", new_actor_id="actor-a1",
                         display_name="甲一账号", role="delegate", organization_id="org-a")
        s.register_actor(request_id="actor-delegate-a3", actor_id="admin", new_actor_id="actor-a3",
                         display_name="甲三账号", role="delegate", organization_id="org-a")
        s.register_site(request_id="site", actor_id="op", site_id="site-1", organization_id="org-host",
                        name="会展中心", timezone_name="Asia/Shanghai")
        s.register_room(request_id="room-secure", actor_id="op", room_id="room-1", site_id="site-1",
                        name="保密会议室一", capacity=2, secure=True)
        s.register_room(request_id="room-open", actor_id="op", room_id="room-2", site_id="site-1",
                        name="开放洽谈室", capacity=3, secure=False)
        s.register_room(request_id="room-single", actor_id="op", room_id="room-3", site_id="site-1",
                        name="保密会议室二", capacity=1, secure=True)
        s.register_delegation(request_id="del-a", actor_id="op", delegation_id="del-a",
                              organization_id="org-a", name="甲国代表团")
        s.register_delegation(request_id="del-b", actor_id="op", delegation_id="del-b",
                              organization_id="org-b", name="乙企业代表团")
        s.register_delegate(request_id="d-a1", actor_id="liaison-a", delegate_id="d-a1",
                            delegation_id="del-a", display_name="甲一", clearance_level=4,
                            languages=["zh", "en"], actor_link="actor-a1", availability=WINDOW)
        s.register_delegate(request_id="d-a2", actor_id="liaison-a", delegate_id="d-a2",
                            delegation_id="del-a", display_name="甲二", clearance_level=2,
                            languages=["zh"], availability=WINDOW)
        s.register_delegate(request_id="d-a3", actor_id="liaison-a", delegate_id="d-a3",
                            delegation_id="del-a", display_name="甲三", clearance_level=4,
                            languages=["zh", "fr"], actor_link="actor-a3", availability=WINDOW)
        s.register_delegate(request_id="d-b1", actor_id="op", delegate_id="d-b1",
                            delegation_id="del-b", display_name="乙一", clearance_level=3,
                            languages=["en"], availability=WINDOW)
        s.register_delegate(request_id="d-b2", actor_id="op", delegate_id="d-b2",
                            delegation_id="del-b", display_name="乙二", clearance_level=4,
                            languages=["en", "zh"], availability=WINDOW)
        s.register_interpreter(request_id="it-1", actor_id="op", interpreter_id="it-1",
                               site_id="site-1", display_name="中英翻译", languages=["zh", "en"],
                               clearance_level=4, actor_link="interp-actor")
        s.register_interpreter(request_id="it-2", actor_id="op", interpreter_id="it-2",
                               site_id="site-1", display_name="中英翻译二", languages=["zh", "en"],
                               clearance_level=4)

    def tearDown(self):
        self.database.close()

    def make_session(self, session_id="sess-1", room_id="room-1", sensitivity=3,
                     start=SESSION_START, end=SESSION_END, languages=("zh",),
                     exclusion_group=None, response_ttl=3600, hold_ttl=7200, observer=True,
                     interpreter="it-1", observer_actor="observer"):
        self.service.create_session(
            request_id=f"cs-{session_id}", actor_id="op", session_id=session_id, site_id="site-1",
            topic=f"闭门会-{session_id}", sensitivity_level=sensitivity, start_at=start, end_at=end,
            room_id=room_id, required_languages=list(languages), exclusion_group=exclusion_group,
            response_ttl_seconds=response_ttl, hold_ttl_seconds=hold_ttl)
        for index, language in enumerate(languages):
            self.service.assign_interpreter(request_id=f"ai-{session_id}-{language}", actor_id="op",
                                            session_id=session_id, interpreter_id=interpreter,
                                            language=language)
        if observer:
            self.service.assign_observer(request_id=f"ao-{session_id}", actor_id="op",
                                         session_id=session_id, observer_actor_id=observer_actor)
        self.service.publish_session(request_id=f"pub-{session_id}", actor_id="op", session_id=session_id)
        return session_id

    def invite(self, delegate_id, session_id="sess-1", actor_id="op"):
        receipt = self.service.invite_delegate(request_id=f"inv-{session_id}-{delegate_id}",
                                               actor_id=actor_id, session_id=session_id,
                                               delegate_id=delegate_id)
        return receipt.resource_id

    def accept(self, invitation_id, actor_id="op", suffix=""):
        return self.service.respond_invitation(request_id=f"acc-{invitation_id}{suffix}",
                                               actor_id=actor_id, invitation_id=invitation_id,
                                               decision="accept")

    def invitation_of(self, delegate_id, session_id="sess-1"):
        row = self.database.connection.execute(
            "SELECT * FROM invitations WHERE session_id=? AND delegate_id=? ORDER BY invited_at DESC, "
            "invitation_id DESC LIMIT 1", (session_id, delegate_id)).fetchone()
        return dict(row) if row else None

    def session_status(self, session_id="sess-1"):
        row = self.database.connection.execute("SELECT status FROM sessions WHERE session_id=?",
                                               (session_id,)).fetchone()
        return row["status"]


class EligibilityTest(MeetingTestBase):
    def test_clearance_insufficient_is_recorded_and_explainable(self):
        self.make_session()
        with self.assertRaises(ConflictError) as ctx:
            self.invite("d-a2")  # 密级 2 < 议题 3
        self.assertIn("clearance_insufficient", str(ctx.exception))
        rejections = self.service.list_rejections(actor_id="op", session_id="sess-1")
        self.assertEqual(1, len(rejections))
        self.assertEqual("clearance_insufficient", rejections[0]["reason_code"])
        self.assertEqual({"required": 3, "actual": 2}, rejections[0]["detail"])
        # 联络员可按本机构查询，向被拒代表解释
        own = self.service.list_rejections(actor_id="liaison-a", session_id="sess-1")
        self.assertEqual(1, len(own))
        # 其他机构联络员看不到
        self.service.register_actor(request_id="actor-liaison-b", actor_id="admin",
                                    new_actor_id="liaison-b", display_name="乙联络员",
                                    role="liaison", organization_id="org-b")
        self.assertEqual([], self.service.list_rejections(actor_id="liaison-b", session_id="sess-1"))

    def test_availability_window_mismatch(self):
        self.service.register_delegate(
            request_id="d-b3", actor_id="op", delegate_id="d-b3", delegation_id="del-b",
            display_name="乙三", clearance_level=4, languages=["en"],
            availability=[{"start_at": "2026-10-05T00:00:00+08:00",
                           "end_at": "2026-10-06T00:00:00+08:00"}])  # 不覆盖 10-04 上午
        self.make_session()
        with self.assertRaises(ConflictError) as ctx:
            self.invite("d-b3")
        self.assertIn("availability_mismatch", str(ctx.exception))

    def test_recusal_between_delegations_blocks_invite(self):
        self.service.register_recusal(request_id="rec-1", actor_id="op", subject_type="delegate",
                                      subject_id="d-b1", counterparty_organization_id="org-a",
                                      reason="利益冲突审查中")
        self.make_session()
        self.invite("d-a1")
        with self.assertRaises(ConflictError) as ctx:
            self.invite("d-b1")
        self.assertIn("recusal_conflict", str(ctx.exception))
        rejections = self.service.list_rejections(actor_id="op", session_id="sess-1")
        self.assertEqual("recusal_conflict", rejections[0]["reason_code"])
        self.assertEqual("org-a", rejections[0]["detail"]["with_organization"])

    def test_observer_recusal_blocks_invite(self):
        self.service.register_recusal(request_id="rec-obs", actor_id="observer",
                                      subject_type="actor", subject_id="observer",
                                      counterparty_organization_id="org-b", reason="曾任职乙企业")
        self.make_session()
        with self.assertRaises(ConflictError) as ctx:
            self.invite("d-b2")
        self.assertIn("recusal_conflict", str(ctx.exception))

    def test_exclusion_group_blocks_second_accept(self):
        self.make_session("sess-1", exclusion_group="minister-track")
        self.make_session("sess-2", room_id="room-2", sensitivity=2,
                          start="2026-10-04T14:00:00+08:00", end="2026-10-04T16:00:00+08:00",
                          exclusion_group="minister-track")
        first = self.invite("d-a1", "sess-1")
        second = self.invite("d-a1", "sess-2")
        self.accept(first)
        # 接受同一互斥组的第二场时被拒并记录
        with self.assertRaises(ConflictError) as ctx:
            self.accept(second)
        self.assertIn("exclusion_conflict", str(ctx.exception))
        rejections = self.service.list_rejections(actor_id="op", delegate_id="d-a1")
        self.assertEqual("accept", rejections[0]["action"])
        self.assertEqual("sess-1", rejections[0]["detail"]["other_session_id"])

    def test_time_overlap_blocks_second_accept(self):
        self.make_session("sess-1")
        self.make_session("sess-2", room_id="room-2", sensitivity=2,
                          start="2026-10-04T10:00:00+08:00", end="2026-10-04T12:00:00+08:00",
                          interpreter="it-2", observer_actor="observer-2")
        first = self.invite("d-a1", "sess-1")
        second = self.invite("d-a1", "sess-2")
        self.accept(first)
        with self.assertRaises(ConflictError):
            self.accept(second)

    def test_secure_room_required_for_sensitive_session(self):
        with self.assertRaises(ValidationError):
            self.service.create_session(
                request_id="cs-bad", actor_id="op", session_id="sess-bad", site_id="site-1",
                topic="敏感议题", sensitivity_level=3, start_at=SESSION_START, end_at=SESSION_END,
                room_id="room-2", required_languages=["zh"])

    def test_language_coverage_required_to_publish(self):
        self.service.create_session(
            request_id="cs-fr", actor_id="op", session_id="sess-fr", site_id="site-1",
            topic="法语专场", sensitivity_level=2, start_at=SESSION_START, end_at=SESSION_END,
            room_id="room-2", required_languages=["fr"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.publish_session(request_id="pub-fr", actor_id="op", session_id="sess-fr")
        self.assertIn("缺少翻译覆盖", str(ctx.exception))


class ResourceBookingTest(MeetingTestBase):
    def test_room_double_booking_rejected(self):
        self.make_session("sess-1")
        self.service.create_session(
            request_id="cs-2", actor_id="op", session_id="sess-2", site_id="site-1", topic="撞期会",
            sensitivity_level=3, start_at="2026-10-04T10:00:00+08:00",
            end_at="2026-10-04T12:00:00+08:00", room_id="room-1", required_languages=["zh"])
        self.service.assign_interpreter(request_id="ai-2", actor_id="op", session_id="sess-2",
                                        interpreter_id="it-1", language="zh")
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_session(request_id="pub-2", actor_id="op", session_id="sess-2")
        self.assertIn("占用", str(ctx.exception))
        # 只有第一个场次持有有效保留
        holds = self.database.connection.execute(
            "SELECT session_id FROM resource_holds WHERE resource_type='room' AND status='held'"
        ).fetchall()
        self.assertEqual(["sess-1"], [row["session_id"] for row in holds])

    def test_interpreter_double_booking_rejected(self):
        self.make_session("sess-1")
        self.service.create_session(
            request_id="cs-2", actor_id="op", session_id="sess-2", site_id="site-1", topic="撞翻译",
            sensitivity_level=2, start_at="2026-10-04T10:00:00+08:00",
            end_at="2026-10-04T12:00:00+08:00", room_id="room-2", required_languages=["zh"])
        self.service.assign_interpreter(request_id="ai-2", actor_id="op", session_id="sess-2",
                                        interpreter_id="it-1", language="zh")
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_session(request_id="pub-2", actor_id="op", session_id="sess-2")
        self.assertIn("翻译员", str(ctx.exception))

    def test_observer_double_booking_rejected(self):
        self.make_session("sess-1")
        self.service.create_session(
            request_id="cs-2", actor_id="op", session_id="sess-2", site_id="site-1", topic="撞观察员",
            sensitivity_level=2, start_at="2026-10-04T10:00:00+08:00",
            end_at="2026-10-04T12:00:00+08:00", room_id="room-2", required_languages=["zh"])
        self.service.assign_interpreter(request_id="ai-2", actor_id="op", session_id="sess-2",
                                        interpreter_id="it-2", language="zh")
        self.service.assign_observer(request_id="ao-2", actor_id="op", session_id="sess-2",
                                     observer_actor_id="observer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_session(request_id="pub-2", actor_id="op", session_id="sess-2")
        self.assertIn("观察员", str(ctx.exception))

    def test_non_overlapping_sessions_share_resources(self):
        self.make_session("sess-1")
        self.make_session("sess-2", start="2026-10-04T14:00:00+08:00",
                          end="2026-10-04T16:00:00+08:00")
        self.assertEqual("published", self.session_status("sess-2"))


class InvitationLifecycleTest(MeetingTestBase):
    def test_capacity_waitlist_and_promotion_on_decline(self):
        self.make_session()  # room-1 容量 2
        first = self.invite("d-a1")
        second = self.invite("d-b2")
        self.invite("d-a3")  # 候补 1
        waitlisted = self.invitation_of("d-a3")
        self.assertEqual("waitlisted", waitlisted["status"])
        self.assertEqual(1, waitlisted["position"])
        self.service.respond_invitation(request_id="dec-1", actor_id="op",
                                        invitation_id=second, decision="decline")
        promoted = self.invitation_of("d-a3")
        self.assertEqual("invited", promoted["status"])
        self.assertIsNotNone(promoted["response_due_at"])

    def test_response_deadline_expires_and_backfills_in_stable_order(self):
        self.make_session(response_ttl=600)
        self.invite("d-a1")
        self.invite("d-b2")
        self.invite("d-a3")  # 候补 1
        self.service.register_delegate(request_id="d-b3", actor_id="op", delegate_id="d-b3",
                                       delegation_id="del-b", display_name="乙三", clearance_level=4,
                                       languages=["en"], availability=WINDOW)
        self.invite("d-b3")  # 候补 2
        self.clock.advance(seconds=601)
        stats = self.service.sweep(actor_id="op")
        self.assertEqual(2, stats["expired_invitations"])
        self.assertEqual(2, stats["promoted_invitations"])
        self.assertEqual("expired", self.invitation_of("d-a1")["status"])
        first = self.invitation_of("d-a3")
        second = self.invitation_of("d-b3")
        self.assertEqual("invited", first["status"])
        self.assertEqual("invited", second["status"])
        # 候补位次保持 1、2，到期时间一致且延续
        self.assertEqual((1, 2), (first["position"], second["position"]))
        self.assertEqual(first["response_due_at"], second["response_due_at"])

    def test_hold_deadline_releases_resources_and_reholds_for_next_cohort(self):
        self.make_session(room_id="room-3", response_ttl=3600, hold_ttl=1800)  # 容量 1
        self.invite("d-a1")
        self.invite("d-a3")  # 候补
        self.clock.advance(seconds=1801)  # 超过保留期但未到响应截止
        stats = self.service.sweep(actor_id="op")
        self.assertEqual(1, stats["expired_invitations"])  # d-a1 未确认被截止
        self.assertEqual(1, stats["promoted_invitations"])  # d-a3 递补
        self.assertEqual(1, stats["reheld_sessions"])  # 资源重新保留
        self.assertEqual("expired", self.invitation_of("d-a1")["status"])
        self.assertEqual("invited", self.invitation_of("d-a3")["status"])
        holds = self.database.connection.execute(
            "SELECT status, COUNT(*) AS c FROM resource_holds WHERE session_id='sess-1' GROUP BY status"
        ).fetchall()
        by_status = {row["status"]: row["c"] for row in holds}
        self.assertEqual(3, by_status["expired"])  # 会议室 + 翻译 + 观察员
        self.assertEqual(3, by_status["held"])

    def test_session_confirms_when_all_resolved(self):
        self.make_session()
        first = self.invite("d-a1")
        second = self.invite("d-b2")
        self.accept(first)
        self.assertEqual("published", self.session_status())
        self.accept(second)
        self.assertEqual("confirmed", self.session_status())
        holds = self.database.connection.execute(
            "SELECT DISTINCT status FROM resource_holds WHERE session_id='sess-1'").fetchall()
        self.assertEqual(["confirmed"], [row["status"] for row in holds])

    def test_duplicate_accept_keeps_single_effective_arrangement(self):
        self.make_session()
        invitation = self.invite("d-a1")
        first = self.service.respond_invitation(request_id="same", actor_id="actor-a1",
                                                invitation_id=invitation, decision="accept")
        replay = self.service.respond_invitation(request_id="same", actor_id="actor-a1",
                                                 invitation_id=invitation, decision="accept")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        # 不同请求号的重复确认被拒绝，生效安排只有一个
        with self.assertRaises(ConflictError):
            self.service.respond_invitation(request_id="other", actor_id="actor-a1",
                                            invitation_id=invitation, decision="accept")
        accepted = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM invitations WHERE session_id='sess-1' AND status='accepted'"
        ).fetchone()["c"]
        self.assertEqual(1, accepted)

    def test_delegation_transfer_moves_seat_and_grants(self):
        self.make_session()
        invitation = self.invite("d-a1")
        self.accept(invitation)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-1",
                                  session_id="sess-1", title="谈判底稿", sensitivity_level=3)
        downloaded = self.service.access_material(request_id="dl-1", actor_id="actor-a1",
                                                  material_id="mat-1")
        self.assertEqual("material_access", downloaded.resource_type)
        # 转授权给同代表团的甲三
        receipt = self.service.delegate_invitation(request_id="xfer-1", actor_id="liaison-a",
                                                   invitation_id=invitation, target_delegate_id="d-a3")
        self.assertEqual("delegated", self.invitation_of("d-a1")["status"])
        self.assertEqual("accepted", self.invitation_of("d-a3")["status"])
        # 原代表授权收回，新代表获得授权
        with self.assertRaises(PermissionDenied):
            self.service.access_material(request_id="dl-2", actor_id="actor-a1", material_id="mat-1")
        self.service.access_material(request_id="dl-3", actor_id="actor-a3", material_id="mat-1")
        trace = self.service.trace_session(actor_id="op", session_id="sess-1")
        grants = trace["materials"][0]["grants"]
        self.assertEqual(2, len(grants))
        self.assertEqual("revoked", grants[0]["status"])
        self.assertEqual("delegated", grants[0]["revoke_reason"])
        self.assertEqual("active", grants[1]["status"])

    def test_delegation_transfer_rejects_other_delegation(self):
        self.make_session()
        invitation = self.invite("d-a1")
        with self.assertRaises(ValidationError):
            self.service.delegate_invitation(request_id="xfer-bad", actor_id="op",
                                             invitation_id=invitation, target_delegate_id="d-b1")

    def test_withdraw_releases_seat_and_promotes(self):
        self.make_session()
        first = self.invite("d-a1")
        self.invite("d-b2")
        self.invite("d-a3")  # 候补
        self.accept(first)
        self.service.withdraw_invitation(request_id="wd-1", actor_id="actor-a1", invitation_id=first)
        self.assertEqual("withdrawn", self.invitation_of("d-a1")["status"])
        self.assertEqual("invited", self.invitation_of("d-a3")["status"])

    def test_replaced_delegate_loses_material_access(self):
        self.make_session()
        invitation = self.invite("d-a1")
        self.accept(invitation)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-1",
                                  session_id="sess-1", title="敏感材料", sensitivity_level=3)
        self.service.access_material(request_id="dl-1", actor_id="actor-a1", material_id="mat-1")
        # 临时替换代表：旧代表停用、授权收回、账号停用
        self.service.replace_delegate(request_id="rep-1", actor_id="liaison-a", delegate_id="d-a1",
                                      new_delegate_id="d-a1b", display_name="甲一（接替）",
                                      clearance_level=4, languages=["zh"], availability=WINDOW)
        with self.assertRaises(PermissionDenied):
            self.service.access_material(request_id="dl-2", actor_id="actor-a1", material_id="mat-1")
        self.assertEqual("revoked", self.invitation_of("d-a1")["status"])
        # 访问事实仍然保留
        trace = self.service.trace_session(actor_id="op", session_id="sess-1")
        self.assertEqual(1, len(trace["materials"][0]["accesses"]))

    def test_reschedule_reopens_confirmation_and_rotates_grants(self):
        self.make_session()
        invitation = self.invite("d-a1")
        self.accept(invitation)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-1",
                                  session_id="sess-1", title="议程草案", sensitivity_level=3)
        self.assertEqual("confirmed", self.session_status())
        self.service.reschedule_session(request_id="rs-1", actor_id="op", session_id="sess-1",
                                        start_at="2026-10-04T15:00:00+08:00",
                                        end_at="2026-10-04T17:00:00+08:00")
        self.assertEqual("published", self.session_status())
        reopened = self.invitation_of("d-a1")
        self.assertEqual("invited", reopened["status"])
        # 旧版本授权已收回，新版本确认前不能下载
        with self.assertRaises(PermissionDenied):
            self.service.access_material(request_id="dl-x", actor_id="actor-a1", material_id="mat-1")
        self.accept(reopened["invitation_id"], suffix="-2")
        self.service.access_material(request_id="dl-y", actor_id="actor-a1", material_id="mat-1")
        trace = self.service.trace_session(actor_id="op", session_id="sess-1")
        grants = trace["materials"][0]["grants"]
        self.assertEqual({1, 2}, {g["basis"]["session_version"] for g in grants})
        self.assertEqual("rescheduled", grants[0]["revoke_reason"])
        self.assertEqual(2, trace["session"]["version"])

    def test_material_grant_respects_material_sensitivity(self):
        self.make_session()
        invitation = self.invite("d-b1")  # 密级 3
        self.accept(invitation)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-low",
                                  session_id="sess-1", title="公开议程", sensitivity_level=2)
        self.service.add_material(request_id="m2", actor_id="op", material_id="mat-high",
                                  session_id="sess-1", title="绝密附件", sensitivity_level=4)
        self.service.register_actor(request_id="actor-b1", actor_id="admin", new_actor_id="actor-b1",
                                    display_name="乙一账号", role="delegate", organization_id="org-b")
        self.database.connection.execute(
            "UPDATE delegates SET actor_id='actor-b1' WHERE delegate_id='d-b1'")
        self.service.access_material(request_id="dl-low", actor_id="actor-b1", material_id="mat-low")
        with self.assertRaises(PermissionDenied):
            self.service.access_material(request_id="dl-high", actor_id="actor-b1",
                                         material_id="mat-high")


class CompletionAndPersistenceTest(MeetingTestBase):
    def test_checkin_and_completion_freeze_facts(self):
        self.make_session()
        invitation = self.invite("d-a1")
        self.accept(invitation)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-1",
                                  session_id="sess-1", title="纪要", sensitivity_level=3)
        self.service.access_material(request_id="dl-1", actor_id="actor-a1", material_id="mat-1")
        self.clock.advance(minutes=31)  # 08:31 北京时间，签到窗口内
        self.service.check_in(request_id="ck-1", actor_id="op", session_id="sess-1", delegate_id="d-a1")
        with self.assertRaises(ConflictError):
            self.service.check_in(request_id="ck-2", actor_id="op", session_id="sess-1",
                                  delegate_id="d-a1")
        self.clock.advance(hours=3)  # 越过结束时间
        stats = self.service.sweep(actor_id="op")
        self.assertEqual(1, stats["completed_sessions"])
        self.assertEqual("completed", self.session_status())
        # 已结束场次不能再邀请，签到与访问事实不被覆盖
        with self.assertRaises(ConflictError):
            self.invite("d-b2")
        trace = self.service.trace_session(actor_id="auditor", session_id="sess-1")
        self.assertEqual(1, len(trace["checkins"]))
        self.assertEqual(1, len(trace["materials"][0]["accesses"]))
        self.assertEqual("accepted", [p for p in trace["participants"]
                                      if p["delegate_id"] == "d-a1"][0]["status"])

    def test_restart_preserves_waitlist_positions_and_deadlines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meeting.sqlite3"
            clock = ManualClock(START)
            database = Database(path)
            service = MeetingService(database, clock)
            service.register_organization(request_id="o1", actor_id="bootstrap",
                                          organization_id="org-host", name="主办方")
            service.register_actor(request_id="a1", actor_id="bootstrap", new_actor_id="admin",
                                   display_name="管理员", role="admin", organization_id="org-host")
            service.register_actor(request_id="a2", actor_id="admin", new_actor_id="op",
                                   display_name="联络", role="operator", organization_id="org-host")
            service.register_site(request_id="s1", actor_id="op", site_id="site-1",
                                  organization_id="org-host", name="展馆", timezone_name="Asia/Shanghai")
            service.register_room(request_id="r1", actor_id="op", room_id="room-1", site_id="site-1",
                                  name="保密会议室", capacity=1, secure=True)
            service.register_organization(request_id="o2", actor_id="admin",
                                          organization_id="org-a", name="甲国馆")
            service.register_delegation(request_id="d1", actor_id="op", delegation_id="del-a",
                                        organization_id="org-a", name="甲国代表团")
            for index in (1, 2):
                service.register_delegate(request_id=f"dg{index}", actor_id="op",
                                          delegate_id=f"d-{index}", delegation_id="del-a",
                                          display_name=f"代表{index}", clearance_level=4,
                                          languages=["zh"], availability=WINDOW)
            service.register_interpreter(request_id="i1", actor_id="op", interpreter_id="it-1",
                                         site_id="site-1", display_name="翻译", languages=["zh"],
                                         clearance_level=4)
            service.create_session(request_id="cs", actor_id="op", session_id="sess-1",
                                   site_id="site-1", topic="闭门会", sensitivity_level=3,
                                   start_at=SESSION_START, end_at=SESSION_END, room_id="room-1",
                                   required_languages=["zh"], response_ttl_seconds=600,
                                   hold_ttl_seconds=7200)
            service.assign_interpreter(request_id="ai", actor_id="op", session_id="sess-1",
                                       interpreter_id="it-1", language="zh")
            service.publish_session(request_id="pub", actor_id="op", session_id="sess-1")
            service.invite_delegate(request_id="inv1", actor_id="op", session_id="sess-1",
                                    delegate_id="d-1")
            service.invite_delegate(request_id="inv2", actor_id="op", session_id="sess-1",
                                    delegate_id="d-2")
            before = database.connection.execute(
                "SELECT response_due_at FROM invitations WHERE delegate_id='d-1'").fetchone()[0]
            database.close()
            # 进程重启：状态从 SQLite 恢复，候补位次与到期时间延续
            later = ManualClock(datetime(2026, 10, 4, 0, 11, tzinfo=timezone.utc))
            database2 = Database(path)
            service2 = MeetingService(database2, later)
            stats = service2.sweep(actor_id="op")
            self.assertEqual(1, stats["expired_invitations"])
            self.assertEqual(1, stats["promoted_invitations"])
            rows = database2.connection.execute(
                "SELECT delegate_id, status, position, response_due_at FROM invitations "
                "ORDER BY invited_at").fetchall()
            self.assertEqual("expired", rows[0]["status"])
            self.assertEqual(before, rows[0]["response_due_at"])  # 原到期时间未被改写
            self.assertEqual("invited", rows[1]["status"])
            self.assertEqual(1, rows[1]["position"])
            self.assertEqual("2026-10-04T00:21:00Z", rows[1]["response_due_at"])
            database2.close()


class ConcurrencyTest(MeetingTestBase):
    def test_concurrent_publish_keeps_single_effective_arrangement(self):
        for sid in ("sess-x", "sess-y"):
            self.service.create_session(
                request_id=f"cs-{sid}", actor_id="op", session_id=sid, site_id="site-1",
                topic="撞期会", sensitivity_level=3, start_at=SESSION_START, end_at=SESSION_END,
                room_id="room-1", required_languages=["zh"])
            self.service.assign_interpreter(request_id=f"ai-{sid}", actor_id="op", session_id=sid,
                                            interpreter_id="it-1", language="zh")
        results: dict[str, str] = {}

        def publish(session_id: str) -> None:
            try:
                self.service.publish_session(request_id=f"pub-{session_id}", actor_id="op",
                                             session_id=session_id)
                results[session_id] = "ok"
            except ConflictError:
                results[session_id] = "conflict"

        threads = [threading.Thread(target=publish, args=(sid,)) for sid in ("sess-x", "sess-y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 同时发布只保留一个生效安排
        self.assertEqual(["conflict", "ok"], sorted(results.values()))
        published = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE status='published'").fetchone()["c"]
        self.assertEqual(1, published)
        holds = self.database.connection.execute(
            "SELECT COUNT(DISTINCT session_id) AS c FROM resource_holds WHERE status='held'"
        ).fetchone()["c"]
        self.assertEqual(1, holds)


class RoleViewTest(MeetingTestBase):
    def setUp(self):
        super().setUp()
        self.make_session()
        self.first = self.invite("d-a1")
        self.invite("d-b2")
        self.accept(self.first)
        self.service.add_material(request_id="m1", actor_id="op", material_id="mat-1",
                                  session_id="sess-1", title="底稿", sensitivity_level=3)

    def test_operator_sees_full_view(self):
        view = self.service.get_session_view(actor_id="op", session_id="sess-1")
        self.assertIn("participants", view)
        self.assertIn("resources", view)
        self.assertEqual("Asia/Shanghai", view["timezone"])
        self.assertEqual("2026-10-04T09:00:00+08:00", view["local_start_at"])

    def test_liaison_sees_only_own_delegation(self):
        view = self.service.get_session_view(actor_id="liaison-a", session_id="sess-1")
        self.assertNotIn("participants", view)
        self.assertEqual(["d-a1"], [row["delegate_id"] for row in view["own_invitations"]])
        self.service.register_actor(request_id="actor-liaison-c", actor_id="admin",
                                    new_actor_id="liaison-c", display_name="丙联络员",
                                    role="liaison", organization_id="org-host")
        # 机构未被邀请的联络员视为不可见
        self.service.register_organization(request_id="org-c", actor_id="admin",
                                           organization_id="org-c", name="丙机构")
        self.database.connection.execute(
            "UPDATE actors SET organization_id='org-c' WHERE actor_id='liaison-c'")
        with self.assertRaises(NotFoundError):
            self.service.get_session_view(actor_id="liaison-c", session_id="sess-1")

    def test_delegate_sees_own_invitation_and_granted_materials(self):
        view = self.service.get_session_view(actor_id="actor-a1", session_id="sess-1")
        self.assertEqual("accepted", view["own_invitation"]["status"])
        self.assertEqual(["mat-1"], [m["material_id"] for m in view["materials"]])
        with self.assertRaises(NotFoundError):
            self.service.get_session_view(actor_id="actor-a3", session_id="sess-1")

    def test_interpreter_sees_assignment_only(self):
        view = self.service.get_session_view(actor_id="interp-actor", session_id="sess-1")
        self.assertEqual("zh", view["assignment"]["language"])
        self.assertNotIn("participants", view)
        assignments = self.service.my_assignments(actor_id="interp-actor")
        self.assertEqual(["sess-1"], [a["session_id"] for a in assignments])
        observer_view = self.service.my_assignments(actor_id="observer")
        self.assertEqual("observer", observer_view[0]["staff_type"])

    def test_trace_requires_staff_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.trace_session(actor_id="liaison-a", session_id="sess-1")
        trace = self.service.trace_session(actor_id="auditor", session_id="sess-1")
        actions = [change["action"] for change in trace["changes"]]
        self.assertIn("session.published", actions)
        self.assertIn("invitation.accepted", actions)
        self.assertIn("grant.issued", actions)


if __name__ == "__main__":
    unittest.main()
