import unittest
from unittest.mock import Mock, patch

import requests
import tennis_court_monitor_all as monitor


def page(*ids):
    html = "".join(
        f'<li class="reserve_box_item"><a href="?resveId={resve_id}">보기</a>'
        f'<div class="reserve_title">테니스장 {resve_id}<div class="reserve_position">기흥구</div></div></li>'
        for resve_id in ids
    )
    return Mock(text=html, status_code=200, url="https://example.test/courts", raise_for_status=Mock())


class YonginListTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.size = patch.object(monitor, "YN_PAGE_SIZE", 2)
        self.sleep = patch.object(monitor.time, "sleep")
        self.size.start()
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.size.stop()

    def test_later_proxy_failure_never_returns_partial_list(self):
        self.session.get.side_effect = [page(1, 2), requests.exceptions.ProxyError("proxy disconnected")]
        with self.assertRaisesRegex(RuntimeError, "2쪽 요청 실패"):
            monitor._yn_fetch_courts(self.session)

    def test_complete_pagination_returns_all_courts(self):
        self.session.get.side_effect = [page(1, 2), page(3)]
        courts = monitor._yn_fetch_courts(self.session)
        self.assertEqual([court["resve_id"] for court in courts], ["1", "2", "3"])

    def test_repeated_page_does_not_loop_forever(self):
        self.session.get.side_effect = [page(1, 2), page(1, 2)]
        with self.assertRaisesRegex(RuntimeError, "같은 목록이 반복"):
            monitor._yn_fetch_courts(self.session)

    def test_waiting_page_is_not_a_successful_empty_list(self):
        self.session.get.return_value = Mock(text="<title>서비스 대기 중</title>", status_code=200,
                                             url="https://example.test/courts", raise_for_status=Mock())
        with self.assertRaisesRegex(RuntimeError, "HTML 파싱 실패"):
            monitor._yn_fetch_courts(self.session)

    def test_failed_fetch_closes_its_session(self):
        self.session.get.side_effect = requests.exceptions.ProxyError("proxy disconnected")
        with patch.object(monitor, "yn_make_session", return_value=self.session):
            with self.assertRaises(RuntimeError):
                monitor.yn_fetch_courts()
        self.session.close.assert_called_once()

    def test_failed_cycle_preserves_previous_snapshot_and_reports_error(self):
        old = [{"court_name": "previous successful data"}]
        with patch.multiple(monitor, _yn_courts=old, _yn_available=old,
                            _yn_last_update="last-success", _yn_status="ok", _yn_error=""), \
             patch.object(monitor, "yn_load_notify_table", return_value={}), \
             patch.object(monitor, "yn_run_once", side_effect=RuntimeError("incomplete court list")), \
             patch.object(monitor, "_notify_if_changed") as notify, \
             patch.object(monitor.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                monitor.yongin_loop()
            data = monitor.app.test_client().get("/api/yongin").get_json()
            self.assertEqual(data["all_courts"], old)
            self.assertEqual(data["last_update"], "last-success")
            self.assertEqual(data["status"], "error")
            self.assertEqual(data["error"], "incomplete court list")
            notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
