"""会谈编排核心领域规则测试。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.scheduling import SchedulingService
from digital_trade_foundation.service import DomainService

from tests._fixtures import build_fixture, make_session


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.database, self.clock, self.base, self.service = build_fixture()

    def tearDown(self):
        self.database.close()

    def advance(self, **delta):
        self.clock._value = self.clock._value + timedelta(**delta)

    def goto(self, *parts):
        self.clock._value = datetime(*parts, tzinfo=timezone.utc)

    # ------------------------------------------------------------ 资格与资源

    def test_clearance_insufficient_rejects_invitation(self):
        make_session(self.service)
        result = self.service.invite(request_id="inv1", actor_id="admin",
                                     session_id="s1", representative_id="p-b3")
        self.assertEqual("rejected", result["decision"])
        self.assertIn("clearance_insufficient", [c["code"] for c in result["conflicts"]])

    def test_venue_double_booking_rejected(self):
        make_session(self.service)
        with self.assertRaises(ConflictError) as ctx:
            make_session(self.service, session_id="s2",
                         starts_at="2026-11-05T09:30:00Z", ends_at="2026-11-05T10:30:00Z")
        self.assertIn("venue_unavailable", str(ctx.exception))

    def test_room_sensitivity_must_cover_session(self):
        with self.assertRaises(ValidationError):
            make_session(self.service, session_id="s2", venue_id="room-open",
                         starts_at="2026-11-05T11:00:00Z", ends_at="2026-11-05T12:00:00Z")

    def test_resource_block_prevents_session(self):
        self.service.add_resource_block(request_id="blk1", actor_id="admin", resource_id="room-secret",
                                        starts_at="2026-11-05T08:30:00Z",
                                        ends_at="2026-11-05T09:30:00Z", reason="设备维护")
        with self.assertRaises(ConflictError) as ctx:
            make_session(self.service)
        self.assertIn("resource_blocked", str(ctx.exception))

    def test_observer_capacity_enforced_across_overlapping_sessions(self):
        make_session(self.service)
        # 第二场在不重叠的时间可以复用观察员；重叠则超过容量 1。
        make_session(self.service, session_id="s2",
                     starts_at="2026-11-05T10:00:00Z", ends_at="2026-11-05T11:00:00Z")
        with self.assertRaises(ConflictError) as ctx:
            make_session(self.service, session_id="s3",
                         starts_at="2026-11-05T09:30:00Z", ends_at="2026-11-05T10:30:00Z")
        self.assertIn("observer_capacity_exceeded", str(ctx.exception))

    def test_missing_interpreter_language_rejected(self):
        with self.assertRaises(ConflictError) as ctx:
            make_session(self.service, session_id="s2",
                         starts_at="2026-11-05T11:00:00Z", ends_at="2026-11-05T12:00:00Z",
                         language_codes=["fr"], resource_ids=["interp"])
        self.assertIn("interpreter_language_missing", str(ctx.exception))

    def test_interpreter_reused_in_non_overlapping_windows(self):
        make_session(self.service)
        make_session(self.service, session_id="s2",
                     starts_at="2026-11-05T10:30:00Z", ends_at="2026-11-05T11:30:00Z")

    # ------------------------------------------------------------ 个人冲突

    def test_time_overlap_rejected(self):
        make_session(self.service, resource_ids=["interp"])
        make_session(self.service, session_id="s2", venue_id="room-ctrl", sensitivity="controlled",
                     starts_at="2026-11-05T09:30:00Z", ends_at="2026-11-05T10:30:00Z",
                     language_codes=["en"], resource_ids=["interp-en"])
        self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        result = self.service.invite(request_id="i2", actor_id="admin", session_id="s2",
                                     representative_id="p-a1")
        self.assertEqual("rejected", result["decision"])
        self.assertIn("time_overlap", [c["code"] for c in result["conflicts"]])

    def test_explicit_exclusion_rejected_even_without_overlap(self):
        make_session(self.service)
        make_session(self.service, session_id="s2", venue_id="room-ctrl", sensitivity="controlled",
                     starts_at="2026-11-05T11:00:00Z", ends_at="2026-11-05T12:00:00Z",
                     language_codes=["en"], resource_ids=["interp-en"])
        self.service.add_exclusion(request_id="ex1", actor_id="admin",
                                   session_id_a="s1", session_id_b="s2")
        self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        result = self.service.invite(request_id="i2", actor_id="admin", session_id="s2",
                                     representative_id="p-a1")
        self.assertIn("exclusive_session", [c["code"] for c in result["conflicts"]])

    def test_exclusive_group_conflict(self):
        make_session(self.service, exclusive_group="track-x", resource_ids=["interp"])
        make_session(self.service, session_id="s2", venue_id="room-ctrl", sensitivity="controlled",
                     starts_at="2026-11-05T15:00:00Z", ends_at="2026-11-05T16:00:00Z",
                     language_codes=["en"], resource_ids=["interp-en"], exclusive_group="track-x")
        self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        result = self.service.invite(request_id="i2", actor_id="admin", session_id="s2",
                                     representative_id="p-a1")
        self.assertIn("exclusive_group", [c["code"] for c in result["conflicts"]])

    def test_recusal_both_directions(self):
        make_session(self.service, capacity=4)
        self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        # p-a2 主动回避 p-a1
        self.service.add_recusal(request_id="rc1", actor_id="admin", representative_id="p-a2",
                                 scope_type="representative", scope_value="p-a1", reason="亲属")
        result = self.service.invite(request_id="i2", actor_id="admin", session_id="s1",
                                     representative_id="p-a2")
        self.assertIn("recusal", [c["code"] for c in result["conflicts"]])

    def test_recusal_by_organization(self):
        make_session(self.service, capacity=4)
        self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        self.service.add_recusal(request_id="rc2", actor_id="admin", representative_id="p-b2",
                                 scope_type="organization", scope_value="org-a", reason="尽调回避")
        result = self.service.invite(request_id="i2", actor_id="admin", session_id="s1",
                                     representative_id="p-b2")
        conflicts = result["conflicts"]
        self.assertTrue(any(c["code"] == "recusal" and c["other_representative_id"] == "p-a1"
                            for c in conflicts), conflicts)

    def test_conflict_preview_is_read_only(self):
        make_session(self.service)
        conflicts = self.service.preview_conflicts(
            actor_id="admin", session_id="s1", representative_id="p-b3")
        self.assertTrue(conflicts)
        seats = self.service.my_schedule("p-b3", actor_id="liaison-b")
        self.assertEqual([], seats)

    # ------------------------------------------------------------ 候补与保留

    def test_waitlist_stable_order_and_promotion(self):
        make_session(self.service, capacity=1)
        first = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                    representative_id="p-a1")
        second = self.service.invite(request_id="i2", actor_id="admin", session_id="s1",
                                     representative_id="p-a2")
        third = self.service.invite(request_id="i3", actor_id="admin", session_id="s1",
                                    representative_id="p-a3")
        self.assertEqual("invited", first["decision"])
        self.assertEqual(1, second["waitlist_rank"])
        self.assertEqual(2, third["waitlist_rank"])
        self.service.respond_invitation(request_id="acc1", actor_id="liaison-a",
                                        seat_id=first["seat_id"], accept=False)
        schedule = {item.session_id: item for item in
                    self.service.my_schedule("p-a2", actor_id="liaison-a")}
        self.assertEqual("invited", schedule["s1"].state)
        # 第三位仍在候补，位置不变
        sched_a3 = self.service.my_schedule("p-a3", actor_id="liaison-a")
        self.assertEqual("waitlisted", sched_a3[0].state)
        self.assertEqual(2, sched_a3[0].waitlist_rank)

    def test_hold_expiry_returns_to_waitlist_keeping_rank(self):
        make_session(self.service, capacity=1)
        invited = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                      representative_id="p-a1")
        waiting = self.service.invite(request_id="i2", actor_id="admin", session_id="s1",
                                      representative_id="p-a2")
        self.advance(minutes=61)
        result = self.service.run_due(request_id="run1", actor_id="admin")
        expired_ids = {item["seat_id"] for item in result["expired"]}
        self.assertIn(invited["seat_id"], expired_ids)
        # 超时者回到候补，且排在原候补之后（新位置稳定分配）
        items = {item.seat_id: item for item in
                 self.service.my_schedule("p-a1", actor_id="liaison-a")}
        self.assertEqual("waitlisted", items[invited["seat_id"]].state)
        self.assertGreater(items[invited["seat_id"]].waitlist_rank, waiting["waitlist_rank"])

    def test_acceptance_after_hold_expiry_rejected(self):
        make_session(self.service, capacity=1)
        invited = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                      representative_id="p-a1")
        self.advance(minutes=61)
        with self.assertRaises(ConflictError):
            self.service.respond_invitation(request_id="acc-late", actor_id="liaison-a",
                                            seat_id=invited["seat_id"], accept=True)

    def test_invitation_idempotent_replay(self):
        make_session(self.service)
        first = self.service.invite(request_id="dup", actor_id="admin", session_id="s1",
                                    representative_id="p-a1")
        second = self.service.invite(request_id="dup", actor_id="admin", session_id="s1",
                                     representative_id="p-a1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["seat_id"], second["seat_id"])

    def test_changed_payload_on_same_request_id_conflicts(self):
        make_session(self.service)
        self.service.invite(request_id="dup", actor_id="admin", session_id="s1",
                            representative_id="p-a1")
        with self.assertRaises(ConflictError):
            self.service.invite(request_id="dup", actor_id="admin", session_id="s1",
                                representative_id="p-a2")

    def test_reinvite_after_decline_appends_version_on_same_seat(self):
        make_session(self.service, capacity=1)
        invited = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                      representative_id="p-a1")
        self.service.respond_invitation(request_id="decline", actor_id="liaison-a",
                                        seat_id=invited["seat_id"], accept=False)
        again = self.service.invite(request_id="i1-again", actor_id="admin", session_id="s1",
                                    representative_id="p-a1")
        self.assertEqual("invited", again["decision"])
        self.assertEqual(invited["seat_id"], again["seat_id"])
        history = self.service.seat_history(invited["seat_id"], actor_id="liaison-a")
        states = [item.state for item in history]
        self.assertEqual(["invited", "declined", "invited"], states)

    # ------------------------------------------------------------ 转授权与材料

    def _accepted_seat(self, rep="p-a1"):
        make_session(self.service)
        invited = self.service.invite(request_id=f"i-{rep}", actor_id="admin",
                                      session_id="s1", representative_id=rep)
        self.service.respond_invitation(request_id=f"acc-{rep}", actor_id="liaison-a",
                                        seat_id=invited["seat_id"], accept=True)
        return invited["seat_id"]

    def test_delegation_moves_material_access(self):
        seat_id = self._accepted_seat("p-a1")
        self.service.upload_material(request_id="mat1", actor_id="admin", session_id="s1",
                                     material_id="doc1", title="秘密纪要", sensitivity="secret")
        self.assertTrue(self.service.access_material(
            actor_id="admin", material_id="doc1", representative_id="p-a1")["allowed"])
        result = self.service.delegate(request_id="dlg1", actor_id="liaison-a", seat_id=seat_id,
                                       to_representative_id="p-a2", reason="部长离会")
        with self.assertRaises(PermissionDenied):
            self.service.access_material(actor_id="admin", material_id="doc1",
                                         representative_id="p-a1")
        access = self.service.access_material(actor_id="admin", material_id="doc1",
                                              representative_id="p-a2")
        self.assertTrue(access["allowed"])
        self.assertEqual(result["seat_id"], access["seat_id"])

    def test_delegation_requires_same_delegation(self):
        seat_id = self._accepted_seat("p-a1")
        with self.assertRaises(ValidationError):
            self.service.delegate(request_id="dlg-bad", actor_id="liaison-a", seat_id=seat_id,
                                  to_representative_id="p-b2")

    def test_delegation_requires_clearance(self):
        seat_id = self._accepted_seat("p-a1")
        # p-a4 同团但知悉级别不足
        self.service.register_representative(request_id="rep-low", actor_id="admin",
                                             representative_id="p-a4", delegation_id="del-a",
                                             organization_id="org-a", display_name="低级别",
                                             clearance="open")
        with self.assertRaises(ConflictError):
            self.service.delegate(request_id="dlg-low", actor_id="liaison-a", seat_id=seat_id,
                                  to_representative_id="p-a4")

    def test_revocation_blocked_after_access_fact(self):
        seat_id = self._accepted_seat("p-a1")
        self.service.upload_material(request_id="mat1", actor_id="admin", session_id="s1",
                                     material_id="doc1", title="秘密纪要", sensitivity="secret")
        delegation = self.service.delegate(request_id="dlg1", actor_id="liaison-a",
                                           seat_id=seat_id, to_representative_id="p-a2")
        self.goto(2026, 11, 5, 9, 30)
        self.service.access_material(actor_id="admin", material_id="doc1",
                                     representative_id="p-a2")
        with self.assertRaises(ConflictError):
            self.service.revoke_delegation(request_id="rev1", actor_id="liaison-a",
                                           authorization_id=delegation["authorization_id"])

    def test_revocation_restores_seat_without_facts(self):
        seat_id = self._accepted_seat("p-a1")
        delegation = self.service.delegate(request_id="dlg1", actor_id="liaison-a",
                                           seat_id=seat_id, to_representative_id="p-a2")
        result = self.service.revoke_delegation(request_id="rev1", actor_id="liaison-a",
                                                authorization_id=delegation["authorization_id"])
        self.assertEqual(seat_id, result["seat_id"])
        history = {item.version: item.state for item in
                   self.service.seat_history(seat_id, actor_id="liaison-a")}
        self.assertEqual("accepted", history[max(history)])

    def test_material_revoked_on_withdrawal(self):
        seat_id = self._accepted_seat("p-a1")
        self.service.upload_material(request_id="mat1", actor_id="admin", session_id="s1",
                                     material_id="doc1", title="秘密纪要", sensitivity="secret")
        self.service.withdraw(request_id="wd1", actor_id="liaison-a", seat_id=seat_id)
        with self.assertRaises(PermissionDenied):
            self.service.access_material(actor_id="admin", material_id="doc1",
                                         representative_id="p-a1")

    def test_clearance_downgrade_blocks_download(self):
        self._accepted_seat("p-a2")
        self.service.upload_material(request_id="mat1", actor_id="admin", session_id="s1",
                                     material_id="doc1", title="秘密纪要", sensitivity="secret")
        self.service.update_clearance(request_id="clr1", actor_id="admin",
                                      representative_id="p-a2", clearance="open")
        with self.assertRaises(PermissionDenied):
            self.service.access_material(actor_id="admin", material_id="doc1",
                                         representative_id="p-a2")

    # ------------------------------------------------------------ 改期与结束

    def test_reschedule_into_conflict_rejected(self):
        make_session(self.service)
        # 另一场在 11:00–12:00 已占用同一会议室、译员与观察员。
        make_session(self.service, session_id="s2",
                     starts_at="2026-11-05T11:00:00Z", ends_at="2026-11-05T12:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.reschedule(request_id="rs1", actor_id="admin", session_id="s1",
                                    starts_at="2026-11-05T11:30:00Z",
                                    ends_at="2026-11-05T12:30:00Z")

    def test_reschedule_renews_pending_hold(self):
        make_session(self.service)
        invited = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                      representative_id="p-a1")
        original_expiry = invited["hold_expires_at"]
        self.advance(minutes=30)
        result = self.service.reschedule(request_id="rs1", actor_id="admin", session_id="s1",
                                         starts_at="2026-11-06T09:00:00Z",
                                         ends_at="2026-11-06T10:00:00Z")
        self.assertIn(invited["seat_id"], result["renewed_holds"])
        items = self.service.my_schedule("p-a1", actor_id="liaison-a")
        self.assertGreater(items[0].hold_expires_at, original_expiry)

    def test_conclude_freezes_facts_and_closes_pending(self):
        make_session(self.service, capacity=2)
        accepted = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                       representative_id="p-a1")
        waiting = self.service.invite(request_id="i2", actor_id="admin", session_id="s1",
                                      representative_id="p-a2")
        self.service.respond_invitation(request_id="acc1", actor_id="liaison-a",
                                        seat_id=accepted["seat_id"], accept=True)
        self.goto(2026, 11, 5, 9, 30)
        self.service.check_in(request_id="ci1", actor_id="admin", session_id="s1",
                              representative_id="p-a1")
        self.service.conclude_session(request_id="end1", actor_id="admin", session_id="s1")
        # 候补被关闭，但签到事实保留
        view = self.service.session_view("s1", actor_id="admin")
        states = {p["representative_id"]: p["state"] for p in view.participants}
        self.assertEqual("cancelled", states["p-a2"])
        self.assertTrue(any(fact["fact_type"] == "checked_in" and
                            fact["representative_id"] == "p-a1" for fact in view.facts))
        # 结束后不能再改席位
        with self.assertRaises(ConflictError):
            self.service.withdraw(request_id="wd-late", actor_id="liaison-a",
                                  seat_id=accepted["seat_id"])

    def test_checked_in_fact_survives_later_roster_changes(self):
        make_session(self.service)
        invited = self.service.invite(request_id="i1", actor_id="admin", session_id="s1",
                                      representative_id="p-a1")
        self.service.respond_invitation(request_id="acc1", actor_id="liaison-a",
                                        seat_id=invited["seat_id"], accept=True)
        self.goto(2026, 11, 5, 9, 30)
        self.service.check_in(request_id="ci1", actor_id="admin", session_id="s1",
                              representative_id="p-a1")
        # 签到后退出：事实不被覆盖
        self.service.withdraw(request_id="wd1", actor_id="liaison-a", seat_id=invited["seat_id"])
        view = self.service.session_view("s1", actor_id="admin")
        self.assertTrue(any(fact["fact_type"] == "checked_in" for fact in view.facts))

    # ------------------------------------------------------------ 持久化恢复

    def test_restart_preserves_waitlist_rank_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "restart.sqlite3")
            clock1 = FixedClockType(datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
            db1, _, _, svc1 = build_fixture(clock1._value, path=path)
            make_session(svc1, capacity=1)
            invited = svc1.invite(request_id="i1", actor_id="admin", session_id="s1",
                                  representative_id="p-a1")
            waiting = svc1.invite(request_id="i2", actor_id="admin", session_id="s1",
                                  representative_id="p-a2")
            expiry_before = invited["hold_expires_at"]
            db1.close()

            # 第二次进程：时钟推进 61 分钟，到期处理沿用持久化的原期限。
            clock2 = FixedClockType(datetime(2026, 11, 1, 9, 1, tzinfo=timezone.utc))
            db2, _, _, svc2 = build_fixture(clock2._value, path=path)
            result = svc2.run_due(request_id="run-after-restart", actor_id="admin")
            self.assertTrue(any(item["seat_id"] == invited["seat_id"] for item in result["expired"]))
            # p-a1 到期释放后，原候补第 1 位 p-a2 在同一维护中按原次序递补。
            promoted_ids = {item["seat_id"] for item in result["promoted"]}
            self.assertIn(waiting["seat_id"], promoted_ids)
            item = svc2.my_schedule("p-a2", actor_id="liaison-a")[0]
            self.assertEqual("invited", item.state)
            # 到期时间由首次发放时持久化（08:00 + 60 分钟），不随重启重置
            self.assertEqual("2026-11-01T09:00:00Z", expiry_before)
            db2.close()


def FixedClockType(value):
    from digital_trade_foundation.clock import FixedClock
    return FixedClock(value)


if __name__ == "__main__":
    unittest.main()
