"""并发写入测试：重复确认与同时发布只能产生一个生效安排。"""

import threading
import unittest
from datetime import datetime, timezone

from digital_trade_foundation.errors import ConflictError

from tests._fixtures import build_fixture, make_session


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.database, self.clock, self.base, self.service = build_fixture()
        make_session(self.service, capacity=1)

    def tearDown(self):
        self.database.close()

    def test_parallel_invites_never_oversell_capacity(self):
        reps = ["p-a1", "p-a2", "p-a3"]
        results: dict[str, str] = {}
        errors: list[str] = []
        barrier = threading.Barrier(len(reps))

        def worker(index: int, rep: str):
            barrier.wait()
            try:
                response = self.service.invite(
                    request_id=f"race-{index}", actor_id="admin",
                    session_id="s1", representative_id=rep)
                results[rep] = response["decision"]
            except ConflictError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i, rep))
                   for i, rep in enumerate(reps)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        invited = [rep for rep, decision in results.items() if decision == "invited"]
        waitlisted = [rep for rep, decision in results.items() if decision == "waitlisted"]
        self.assertEqual(1, len(invited), results)
        self.assertEqual(2, len(waitlisted), results)
        self.assertEqual([], errors)

    def test_same_representative_concurrent_double_invite(self):
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def worker(request_id: str):
            barrier.wait()
            try:
                self.service.invite(request_id=request_id, actor_id="admin",
                                    session_id="s1", representative_id="p-a1")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=worker, args=(f"dup-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(["ok", "conflict"]), sorted(outcomes))

    def test_concurrent_accept_and_withdraw_single_effective(self):
        response = self.service.invite(request_id="invite-only", actor_id="admin",
                                       session_id="s1", representative_id="p-a1")
        seat_id = response["seat_id"]
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def accept():
            barrier.wait()
            try:
                self.service.respond_invitation(
                    request_id="accept-1", actor_id="liaison-a", seat_id=seat_id, accept=True)
                outcomes.append("accepted")
            except ConflictError:
                outcomes.append("rejected-accept")

        def withdraw():
            barrier.wait()
            try:
                self.service.withdraw(request_id="withdraw-1", actor_id="liaison-a",
                                      seat_id=seat_id)
                outcomes.append("withdrawn")
            except ConflictError:
                outcomes.append("rejected-withdraw")

        threads = [threading.Thread(target=accept), threading.Thread(target=withdraw)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 无论线程交叠如何，席位最终只有一个确定终态，版本链严格递增、无空洞。
        schedule = self.service.my_schedule("p-a1", actor_id="liaison-a")[0]
        history = self.service.seat_history(seat_id, actor_id="liaison-a")
        self.assertEqual(schedule.state, history[-1].state)
        self.assertEqual([item.version for item in history],
                         list(range(1, len(history) + 1)))
        # 接受后再退出是合法链（invited→accepted→withdrawn）；先退出则接受被拒。
        states = [item.state for item in history]
        self.assertEqual("invited", states[0])
        self.assertIn(states[-1], ("accepted", "withdrawn"))


if __name__ == "__main__":
    unittest.main()
