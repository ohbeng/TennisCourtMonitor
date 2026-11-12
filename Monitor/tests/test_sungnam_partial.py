import unittest
from unittest.mock import Mock, patch

import tennis_court_monitor_all as monitor
from sungnam_client import AccessBlocked
from test_sungnam import TIMETABLE, EMPTY_TIMETABLE


FACILITIES = [
    {"id": facility_id, "name": name, "weekday_times": ["ALL"], "weekend_times": ["ALL"]}
    for facility_id, name in (("FAC26", "탄천실내"), ("FAC61", "수내"), ("FAC99", "야탑"), ("FAC101", "구미"))
]
ACCOUNTS = [{"username": "test", "password": "unused"}]


class PartialSungnamTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.multiple(
            monitor, _sn_available=[], _sn_courts=[], _sn_last_update="", _sn_status="starting",
            _sn_error="", _sn_collected={}, _sn_targets={}, _sn_cycle_done=set(),
        )
        self.state.start()
        self.sleep = patch.object(monitor.time, "sleep")
        self.sleep.start()
        self.session = Mock(authenticated=False, manual_login=False)
        self.session.login.return_value = True
        self.today = monitor.datetime.now(monitor.KST).strftime("%Y-%m-%d")

    def tearDown(self):
        self.sleep.stop()
        self.state.stop()

    def data(self):
        return monitor.build_sungnam_payload()

    def row(self, facility_id, time):
        facility = next(f for f in FACILITIES if f["id"] == facility_id)
        return {"fac_id": facility_id, "facility_name": facility["name"], "date": self.today,
                "day_of_week": "월요일", "court": "1번 코트", "time": time,
                "is_available": True, "reservation_name": ""}

    def test_mid_scan_block_retains_already_collected_timetables(self):
        self.session.get_timetable.side_effect = [TIMETABLE] * 4 + [EMPTY_TIMETABLE, AccessBlocked("blocked")]
        self.session.wait.side_effect = KeyboardInterrupt
        with patch.object(monitor, "_notify_if_changed") as notify, self.assertRaises(KeyboardInterrupt):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
        data = self.data()
        self.assertEqual(data["status"], "blocked")
        self.assertEqual(data["progress"], {"completed": 5, "total": 16})
        self.assertEqual(len(data["all_courts"]), 8)
        self.assertEqual(len(data["available"]), 4)
        self.assertTrue(data["last_update"])
        self.assertEqual(sum(item["status"] == "missing" for item in data["coverage"]), 11)
        notify.assert_not_called()

    def test_first_result_is_published_before_second_request(self):
        requests = []
        def get_timetable(*args):
            requests.append(args)
            if len(requests) == 1:
                return TIMETABLE
            data = self.data()
            self.assertEqual(data["status"], "scanning")
            self.assertEqual(data["progress"]["completed"], 1)
            self.assertEqual(len(data["all_courts"]), 2)
            raise AccessBlocked("blocked")
        self.session.get_timetable.side_effect = get_timetable
        self.session.wait.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, [])
        self.assertEqual(len(self.data()["all_courts"]), 2)

    def test_completed_cycle_reports_full_coverage(self):
        self.session.get_timetable.return_value = TIMETABLE
        self.session.wait.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, [])
        data = self.data()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["progress"], {"completed": 16, "total": 16})
        self.assertTrue(all(item["status"] == "current" for item in data["coverage"]))
        self.assertEqual(len(data["all_courts"]), 32)
        self.assertFalse(any(row.get("is_previous") for row in data["all_courts"]))

    def test_confirmed_empty_result_replaces_old_slots_but_is_not_missing(self):
        monitor._sn_begin_cycle(FACILITIES)
        old = self.row("FAC26", "07:00 ~ 08:50")
        monitor._sn_publish_result("FAC26", self.today, [old], [old])
        monitor._sn_begin_cycle(FACILITIES)
        self.assertTrue(self.data()["all_courts"][0]["is_previous"])
        monitor._sn_publish_result("FAC26", self.today, [], [])
        data = self.data()
        self.assertEqual(data["all_courts"], [])
        coverage = next(item for item in data["coverage"] if item["fac_id"] == "FAC26" and item["date"] == self.today)
        self.assertEqual(coverage["status"], "current")
        self.assertTrue(coverage["last_update"])
        self.assertEqual(data["progress"]["completed"], 1)

    def test_failed_bucket_keeps_old_data_while_successful_bucket_is_updated(self):
        monitor._sn_begin_cycle(FACILITIES)
        for facility_id in ("FAC26", "FAC61"):
            row = self.row(facility_id, "07:00 ~ 08:50")
            monitor._sn_publish_result(facility_id, self.today, [row], [row])
        monitor._sn_begin_cycle(FACILITIES)
        row = self.row("FAC26", "19:00 ~ 20:50")
        monitor._sn_publish_result("FAC26", self.today, [row], [row])
        data = self.data()
        slots = {slot["fac_id"]: slot for slot in data["all_courts"]}
        self.assertEqual(slots["FAC26"]["time"], "19:00 ~ 20:50")
        self.assertFalse(slots["FAC26"]["is_previous"])
        self.assertEqual(slots["FAC61"]["time"], "07:00 ~ 08:50")
        self.assertTrue(slots["FAC61"]["is_previous"])

    def test_old_dates_and_removed_facilities_are_not_displayed(self):
        monitor._sn_begin_cycle(FACILITIES)
        row = self.row("FAC26", "07:00 ~ 08:50")
        monitor._sn_publish_result("FAC26", self.today, [row], [row])
        monitor._sn_collected["FAC26", "2000-01-01"] = monitor._sn_collected["FAC26", self.today]
        monitor._sn_begin_cycle([FACILITIES[1]])
        data = self.data()
        self.assertEqual(data["all_courts"], [])
        self.assertEqual(data["last_update"], "")
        self.assertEqual(monitor._sn_collected, {})
        self.assertEqual(data["progress"], {"completed": 0, "total": 4})

    def test_repeated_result_replaces_bucket_instead_of_duplicating_rows(self):
        monitor._sn_begin_cycle(FACILITIES)
        for time in ("07:00 ~ 08:50", "19:00 ~ 20:50"):
            row = self.row("FAC26", time)
            monitor._sn_publish_result("FAC26", self.today, [row], [row])
        data = self.data()
        self.assertEqual(len(data["all_courts"]), 1)
        self.assertEqual(data["all_courts"][0]["time"], "19:00 ~ 20:50")
        self.assertEqual(data["progress"]["completed"], 1)

    def test_next_scan_starts_with_uncollected_timetables(self):
        monitor._sn_begin_cycle(FACILITIES)
        for facility_id in ("FAC26", "FAC61"):
            row = self.row(facility_id, "07:00 ~ 08:50")
            monitor._sn_publish_result(facility_id, self.today, [row], [row])
        requested = []
        def get_timetable(fac_id, date):
            requested.append((fac_id, date))
            return TIMETABLE
        self.session.get_timetable.side_effect = get_timetable
        self.session.authenticated = True
        monitor.sn_run_once(ACCOUNTS, FACILITIES, session=self.session)
        collected = {("FAC26", self.today), ("FAC61", self.today)}
        self.assertEqual(len(requested), 16)
        self.assertTrue(all(key not in collected for key in requested[:14]))
        self.assertEqual(set(requested[14:]), collected)

    def test_session_is_logged_out_every_fifth_completed_cycle(self):
        self.session.authenticated = True
        self.session.get_timetable.return_value = TIMETABLE
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 6:
                raise KeyboardInterrupt
        self.session.wait.side_effect = wait
        logout_after = []
        self.session.logout.side_effect = lambda: logout_after.append(len(waits))
        with patch.object(monitor, "SN_RELOGIN_EVERY_CYCLES", 5), self.assertRaises(KeyboardInterrupt):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, [])
        self.assertEqual(logout_after, [4])

    def test_manual_login_session_is_never_logged_out_automatically(self):
        self.session.authenticated = True
        self.session.manual_login = True
        self.session.get_timetable.return_value = TIMETABLE
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise KeyboardInterrupt
        self.session.wait.side_effect = wait
        with patch.object(monitor, "SN_RELOGIN_EVERY_CYCLES", 2), self.assertRaises(KeyboardInterrupt):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, [])
        self.session.logout.assert_not_called()



if __name__ == "__main__":
    unittest.main()
