"""会谈编排 HTTP 路由与履职视角测试。"""

import unittest

from digital_trade_foundation.api import route
from digital_trade_foundation.scheduling import SchedulingService
from digital_trade_foundation.service import DomainService

from tests._fixtures import build_fixture, make_session


def h(actor):
    return {"X-Actor-Id": actor}


class SchedulingApiTest(unittest.TestCase):
    def setUp(self):
        self.database, self.clock, self.base, self.service = build_fixture()
        make_session(self.service)

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="admin"):
        return route(self.base, method, path, body or {}, h(actor),
                     scheduling=self.service)

    def test_invitation_round_trip_and_replay_status(self):
        status, payload = self.call("POST", "/invitations", {
            "request_id": "req1", "session_id": "s1", "representative_id": "p-a1"})
        self.assertEqual(201, status)
        self.assertEqual("invited", payload["decision"])
        seat_id = payload["seat_id"]
        status2, payload2 = self.call("POST", "/invitations", {
            "request_id": "req1", "session_id": "s1", "representative_id": "p-a1"})
        self.assertEqual(200, status2)
        self.assertTrue(payload2["replayed"])
        self.assertEqual(seat_id, payload2["seat_id"])

    def test_rejected_invitation_explains_conflict(self):
        status, payload = self.call("POST", "/invitations", {
            "request_id": "req2", "session_id": "s1", "representative_id": "p-b3"})
        self.assertEqual(201, status)
        self.assertEqual("rejected", payload["decision"])
        decision_id = payload["decision_id"]
        status, fetched = self.call("GET", f"/decisions/{decision_id}")
        self.assertEqual(200, status)
        self.assertIn("clearance_insufficient", [c["code"] for c in fetched["conflicts"]])

    def test_liaison_sees_only_own_delegation_in_session_view(self):
        make_session(self.service, session_id="s2", title="采购会", sensitivity="controlled",
                     venue_id="room-ctrl", capacity=4, language_codes=["en"],
                     resource_ids=["interp-en"],
                     starts_at="2026-11-05T14:00:00Z", ends_at="2026-11-05T15:00:00Z")
        self.call("POST", "/invitations",
                  {"request_id": "i-a", "session_id": "s2", "representative_id": "p-a1"})
        self.call("POST", "/invitations",
                  {"request_id": "i-b", "session_id": "s2", "representative_id": "p-b1"})
        _, view_a = self.call("GET", "/sessions/s2/view", actor="liaison-a")
        own = [p for p in view_a["participants"] if p["delegation_id"] == "del-a"]
        other = [p for p in view_a["participants"] if p["delegation_id"] == "del-b"]
        self.assertEqual("甲国代表一", own[0]["display_name"])
        self.assertIsNone(other[0]["organization_id"])
        self.assertEqual("其他代表团代表", other[0]["display_name"])
        # 保密依据仅主办方可见
        self.assertEqual([], view_a["confidentiality_basis"])
        _, admin_view = self.call("GET", "/sessions/s2/view", actor="admin")
        self.assertTrue(admin_view["confidentiality_basis"])

    def test_my_schedule_scoped_to_representative(self):
        self.call("POST", "/invitations",
                  {"request_id": "i-a", "session_id": "s1", "representative_id": "p-a1"})
        status, payload = self.call("GET", "/representatives/p-a1/schedule", actor="liaison-a")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("s1", payload["items"][0]["session_id"])

    def test_liaison_cannot_read_other_delegation_schedule(self):
        status, payload = self.call("GET", "/representatives/p-a1/schedule", actor="liaison-b")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_resource_schedule_org_scoped(self):
        status, payload = self.call("GET", "/resources/interp/schedule", actor="rmgr1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("s1", payload["items"][0]["session_id"])

    def test_conflict_preview_endpoint(self):
        status, payload = self.call("GET",
                                    "/conflicts/preview?session_id=s1&representative_id=p-b3")
        self.assertEqual(200, status)
        self.assertTrue(payload["items"])
        self.assertEqual(400, self.call("GET", "/conflicts/preview?session_id=s1")[0])

    def test_seat_history_endpoint(self):
        _, invited = self.call("POST", "/invitations", {
            "request_id": "i-a", "session_id": "s1", "representative_id": "p-a1"})
        self.call("POST", "/invitations/respond",
                  {"request_id": "acc", "seat_id": invited["seat_id"], "accept": True},
                  actor="liaison-a")
        status, payload = self.call("GET", f"/seats/{invited['seat_id']}/history",
                                    actor="liaison-a")
        self.assertEqual(200, status)
        states = [item["state"] for item in payload["items"]]
        self.assertEqual(["invited", "accepted"], states)

    def test_material_upload_and_access_endpoints(self):
        _, invited = self.call("POST", "/invitations", {
            "request_id": "i-a", "session_id": "s1", "representative_id": "p-a1"})
        self.call("POST", "/invitations/respond",
                  {"request_id": "acc", "seat_id": invited["seat_id"], "accept": True},
                  actor="liaison-a")
        status, payload = self.call("POST", "/materials", {
            "request_id": "mat", "session_id": "s1", "material_id": "doc1",
            "title": "秘密纪要", "sensitivity": "secret"})
        self.assertEqual(201, status)
        status, access = self.call("POST", "/materials/access", {
            "material_id": "doc1", "representative_id": "p-a1"})
        self.assertEqual(200, status)
        self.assertTrue(access["allowed"])
        status, denied = self.call("POST", "/materials/access", {
            "material_id": "doc1", "representative_id": "p-b3"})
        self.assertEqual(403, status)

    def test_unknown_scheduling_route_falls_through_to_404(self):
        status, payload = self.call("GET", "/no-such-thing")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        status, payload = route(self.base, "POST", "/invitations", {
            "request_id": "x", "session_id": "s1", "representative_id": "p-a1"}, {},
            scheduling=self.service)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_auditor_has_read_only_access(self):
        self.base.register_actor(request_id="auditor-actor", actor_id="admin",
                                 new_actor_id="aud1", display_name="审计员",
                                 role="auditor", organization_id="org-a")
        status, payload = self.call("GET", "/sessions/s1/view", actor="aud1")
        self.assertEqual(200, status)
        self.assertIn("history", payload)
        status, payload = self.call("POST", "/invitations", {
            "request_id": "x", "session_id": "s1", "representative_id": "p-a1"}, actor="aud1")
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
