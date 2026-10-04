import unittest
from datetime import datetime, timezone

from digital_trade_foundation.api import route
from digital_trade_foundation.clock import ManualClock
from digital_trade_foundation.scheduling import MeetingService
from digital_trade_foundation.storage import Database


WINDOW = [{"start_at": "2026-10-04T00:00:00+08:00", "end_at": "2026-10-06T00:00:00+08:00"}]


class MeetingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.service = MeetingService(self.database, self.clock)
        self.post("/organizations", {"request_id": "o1", "organization_id": "org-host",
                                     "name": "主办方"}, "bootstrap")
        self.post("/actors", {"request_id": "a1", "new_actor_id": "admin", "display_name": "管理员",
                              "role": "admin", "organization_id": "org-host"}, "bootstrap")
        self.post("/actors", {"request_id": "a2", "new_actor_id": "op", "display_name": "联络",
                              "role": "operator", "organization_id": "org-host"}, "admin")
        self.post("/organizations", {"request_id": "o2", "organization_id": "org-a",
                                     "name": "甲国馆"}, "admin")
        self.post("/actors", {"request_id": "a3", "new_actor_id": "liaison-a",
                              "display_name": "甲联络员", "role": "liaison",
                              "organization_id": "org-a"}, "admin")
        self.post("/actors", {"request_id": "a4", "new_actor_id": "actor-a1",
                              "display_name": "甲一账号", "role": "delegate",
                              "organization_id": "org-a"}, "admin")
        self.post("/sites", {"request_id": "s1", "site_id": "site-1", "organization_id": "org-host",
                             "name": "展馆", "timezone_name": "Asia/Shanghai"}, "op")
        self.post("/rooms", {"request_id": "r1", "room_id": "room-1", "site_id": "site-1",
                             "name": "保密会议室", "capacity": 2, "secure": True}, "op")
        self.post("/delegations", {"request_id": "d1", "delegation_id": "del-a",
                                   "organization_id": "org-a", "name": "甲国代表团"}, "op")
        self.post("/delegates", {"request_id": "dg1", "delegate_id": "d-a1", "delegation_id": "del-a",
                                 "display_name": "甲一", "clearance_level": 4, "languages": ["zh"],
                                 "actor_link": "actor-a1", "availability": WINDOW}, "liaison-a")
        self.post("/interpreters", {"request_id": "i1", "interpreter_id": "it-1", "site_id": "site-1",
                                    "display_name": "翻译", "languages": ["zh"],
                                    "clearance_level": 4}, "op")
        self.post("/sessions", {"request_id": "cs1", "session_id": "sess-1", "site_id": "site-1",
                                "topic": "部长闭门会", "sensitivity_level": 3,
                                "start_at": "2026-10-04T09:00:00+08:00",
                                "end_at": "2026-10-04T11:00:00+08:00", "room_id": "room-1",
                                "required_languages": ["zh"]}, "op")
        self.post("/sessions/sess-1/interpreters", {"request_id": "ai1", "interpreter_id": "it-1",
                                                    "language": "zh"}, "op")
        self.post("/sessions/sess-1/publish", {"request_id": "pub1"}, "op")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor=""):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor})

    def test_invite_respond_and_view_flow(self):
        status, payload = self.post("/sessions/sess-1/invitations",
                                    {"request_id": "inv1", "delegate_id": "d-a1"}, "liaison-a")
        self.assertEqual(201, status)
        invitation_id = payload["resource_id"]
        # 重复提交同一请求号返回原回执
        status, replay = self.post("/sessions/sess-1/invitations",
                                   {"request_id": "inv1", "delegate_id": "d-a1"}, "liaison-a")
        self.assertEqual(200, status)
        self.assertEqual(invitation_id, replay["resource_id"])
        status, _ = self.post(f"/invitations/{invitation_id}/respond",
                              {"request_id": "acc1", "decision": "accept"}, "actor-a1")
        self.assertEqual(201, status)
        status, view = self.get("/sessions/sess-1", "actor-a1")
        self.assertEqual(200, status)
        self.assertEqual("accepted", view["own_invitation"]["status"])
        status, view = self.get("/sessions/sess-1", "liaison-a")
        self.assertEqual(200, status)
        self.assertEqual("d-a1", view["own_invitations"][0]["delegate_id"])
        self.assertNotIn("participants", view)

    def test_rejection_is_explained_over_http(self):
        self.post("/delegates", {"request_id": "dg2", "delegate_id": "d-a2",
                                 "delegation_id": "del-a", "display_name": "甲二",
                                 "clearance_level": 1, "languages": ["zh"]}, "liaison-a")
        status, payload = self.post("/sessions/sess-1/invitations",
                                    {"request_id": "inv2", "delegate_id": "d-a2"}, "liaison-a")
        self.assertEqual(409, status)
        self.assertIn("clearance_insufficient", payload["message"])
        status, payload = self.get("/delegates/d-a2/rejections", "liaison-a")
        self.assertEqual(200, status)
        self.assertEqual("clearance_insufficient", payload["items"][0]["reason_code"])
        self.assertEqual({"required": 3, "actual": 1}, payload["items"][0]["detail"])

    def test_trace_requires_privileged_role(self):
        status, _ = self.get("/sessions/sess-1/trace", "liaison-a")
        self.assertEqual(403, status)
        status, payload = self.get("/sessions/sess-1/trace", "op")
        self.assertEqual(200, status)
        self.assertIn("changes", payload)
        self.assertIn("resources", payload)

    def test_liaison_cannot_touch_other_organization(self):
        self.post("/organizations", {"request_id": "o3", "organization_id": "org-b",
                                     "name": "乙企业"}, "admin")
        self.post("/delegations", {"request_id": "d2", "delegation_id": "del-b",
                                   "organization_id": "org-b", "name": "乙代表团"}, "op")
        self.post("/delegates", {"request_id": "dg3", "delegate_id": "d-b1",
                                 "delegation_id": "del-b", "display_name": "乙一",
                                 "clearance_level": 4, "languages": ["en"]}, "op")
        status, _ = self.post("/sessions/sess-1/invitations",
                              {"request_id": "inv3", "delegate_id": "d-b1"}, "liaison-a")
        self.assertEqual(403, status)

    def test_material_access_over_http(self):
        _, payload = self.post("/sessions/sess-1/invitations",
                               {"request_id": "inv1", "delegate_id": "d-a1"}, "op")
        invitation_id = payload["resource_id"]
        self.post(f"/invitations/{invitation_id}/respond",
                  {"request_id": "acc1", "decision": "accept"}, "actor-a1")
        self.post("/materials", {"request_id": "m1", "material_id": "mat-1", "session_id": "sess-1",
                                 "title": "底稿", "sensitivity_level": 3}, "op")
        status, payload = self.post("/materials/mat-1/access", {"request_id": "dl1"}, "actor-a1")
        self.assertEqual(201, status)
        self.assertEqual("material_access", payload["resource_type"])
        status, _ = self.post("/materials/mat-1/access", {"request_id": "dl2"}, "op")
        self.assertEqual(403, status)

    def test_sweep_endpoint(self):
        status, payload = self.post("/maintenance/sweep", {}, "op")
        self.assertEqual(200, status)
        self.assertIn("expired_invitations", payload)
        status, _ = self.post("/maintenance/sweep", {}, "liaison-a")
        self.assertEqual(403, status)

    def test_my_invitations_scoped_by_role(self):
        self.post("/sessions/sess-1/invitations", {"request_id": "inv1", "delegate_id": "d-a1"}, "op")
        status, payload = self.get("/me/invitations", "actor-a1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, payload = self.get("/me/invitations", "liaison-a")
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
