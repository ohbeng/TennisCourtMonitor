import asyncio
import json
import os
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import tennis_court_monitor_all as monitor
from sungnam_client import (
    AccessBlocked, BrowserLaunchError, LoginFailed, ManualLoginRequired, OperationTimeout, SessionExpired,
    SungnamClient, SungnamError, _human_pause, parse_timetable, validate_timetable,
)


TIMETABLE = """
<p>예약은 로그인 후 가능합니다.</p>
<script>const fallback = "window.location.replace('/auto_detect.do');";</script>
<label class="extra lb-timetable required tit"><span>1번 코트</span></label>
<div class="mgb30 tableBox"><table><tbody>
<tr><td><button>예약 가능</button></td><td>1</td>
    <td><span>7:00</span> ～ <span>08:50</span></td><td>&nbsp;</td></tr>
<tr><td>예약마감</td><td>2</td><td>09:00 ~ 10:50</td><td><b>예약자</b></td></tr>
</tbody></table></div>
"""
EMPTY_TIMETABLE = """
<label class='tit required lb-timetable'>1번 코트</label>
<div class='tableBox mgb30'><table><tbody>
<tr><td colspan='4'>이용가능한 시간이 없습니다</td></tr>
</tbody></table></div>
"""
URL = "https://res.isdc.co.kr/otherTimetable.do"
ACCOUNTS = [{"username": "test", "password": "unused"}]
FACILITIES = [{"id": "FAC26", "name": "탄천실내",
               "weekday_times": ["ALL"], "weekend_times": ["ALL"]}]


class ResponseTests(unittest.TestCase):
    def test_block_page_is_rejected(self):
        html = '<h3>비정상 접근 탐지</h3><p>비정상적인 접근이 탐지되었습니다.</p>'
        with self.assertRaises(AccessBlocked):
            validate_timetable(200, URL, html)

    def test_block_redirect_is_rejected(self):
        with self.assertRaises(AccessBlocked):
            validate_timetable(200, "https://res.isdc.co.kr/auto_detect.do", "")

    def test_authentication_failures_are_explicit(self):
        for status, url, body in (
            (401, URL, ""), (200, "https://res.isdc.co.kr/login.do", ""),
            (200, URL, '<form id="loginForm"><input id="web_id"></form>'),
            (200, URL, "session"),
            (200, URL, "<script>location.href = './login.do';</script>"),
        ):
            with self.subTest(status=status, url=url, body=body):
                with self.assertRaises(SessionExpired):
                    validate_timetable(status, url, body)

    def test_request_limits_do_not_trigger_reauthentication(self):
        for status in (403, 429):
            with self.subTest(status=status), self.assertRaises(AccessBlocked):
                validate_timetable(status, URL, "")

    def test_network_and_unknown_html_are_errors(self):
        for status, body in ((500, "error"), (200, ""), (200, "<p>점검 중</p>")):
            with self.subTest(status=status, body=body), self.assertRaises(SungnamError):
                validate_timetable(status, URL, body)

    def test_login_word_and_security_fallback_script_are_not_failures(self):
        self.assertEqual(validate_timetable(200, URL, TIMETABLE), TIMETABLE)

    def test_legitimate_empty_timetable_is_successful(self):
        self.assertEqual(validate_timetable(200, URL, EMPTY_TIMETABLE), EMPTY_TIMETABLE)
        self.assertEqual(parse_timetable(EMPTY_TIMETABLE), ([], []))

    def test_parsing_tolerates_classes_nested_tags_and_spacing(self):
        available, all_slots = parse_timetable(TIMETABLE)
        self.assertEqual(len(available), 1)
        self.assertEqual(len(all_slots), 2)
        self.assertEqual(available[0]["time"], "07:00 ~ 08:50")
        self.assertEqual(all_slots[0]["court"], "1번 코트")
        self.assertFalse(all_slots[1]["is_available"])
        self.assertEqual(all_slots[1]["reservation_name"], "예약자")

    def test_changed_table_structure_is_not_zero_availability(self):
        for html in (
            '<label class="lb-timetable">코트 이름 변경</label><div class="tableBox"></div>',
            '<label class="lb-timetable">1번 코트</label><div class="tableBox"></div>',
            '<label class="lb-timetable">1번 코트</label><div class="tableBox"><table><tbody><tr><td>변경된 형식</td></tr></tbody></table></div>',
        ):
            with self.subTest(html=html), self.assertRaises(SungnamError):
                parse_timetable(html)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        # 실제 브라우저 대신 Mock을 사용하는 테스트에는 가상의 표시 세션을 준다.
        display = patch.dict(os.environ, {"DISPLAY": ":42"})
        display.start()
        self.addCleanup(display.stop)
        pause = patch("sungnam_client._human_pause", new_callable=AsyncMock)
        self.pause = pause.start()
        self.addCleanup(pause.stop)
        self.client = SungnamClient("unused-test-profile", manual_login=True)
        self.client.page = Mock()

    def tearDown(self):
        self.client.close()

    def test_explicit_manual_login_opens_visible_browser_without_submitting_credentials(self):
        page = Mock(select=AsyncMock())
        browser = Mock(start=AsyncMock(), get=AsyncMock(return_value=page), _process=None, aclose=AsyncMock())
        self.client._wait_state = AsyncMock(return_value={"authenticated": True})
        with patch("sungnam_client.uc.Config") as config, \
             patch("sungnam_client.uc.Browser", return_value=browser):
            self.assertTrue(self.client.login("unused-user", "unused-password"))
        self.assertIs(config.call_args.kwargs["headless"], False)
        self.assertIs(config.call_args.kwargs["sandbox"], True)
        page.select.assert_not_awaited()

    def test_browser_start_failure_is_cleaned_up_and_explained(self):
        browser = Mock(start=AsyncMock(side_effect=RuntimeError("Failed to connect to browser")),
                       _process=None, aclose=AsyncMock())
        with patch("sungnam_client.uc.Config"), patch("sungnam_client.uc.Browser", return_value=browser):
            with self.assertRaises(BrowserLaunchError) as failure:
                self.client.login("", "")
        self.assertIn("프로필", str(failure.exception))
        browser.aclose.assert_awaited_once()
        self.assertIsNone(self.client.browser)

    def test_partial_browser_process_is_closed_when_start_fails(self):
        process = Mock(returncode=1, stderr=Mock(read=AsyncMock(return_value=b'profile in use')))
        browser = Mock(start=AsyncMock(side_effect=RuntimeError("Failed to connect to browser")),
                       _process=process, aclose=AsyncMock())
        with patch("sungnam_client.uc.Config"), patch("sungnam_client.uc.Browser", return_value=browser):
            with self.assertRaises(BrowserLaunchError):
                self.client._run(self.client._start_browser())
        process._transport.close.assert_called_once()
        browser.aclose.assert_awaited_once()

    def test_shutdown_cancels_wait_instead_of_waiting_for_poll_interval(self):
        async def wait_until_stopped():
            self.client.loop.call_soon(self.client.request_stop)
            await asyncio.sleep(90)
        with self.assertRaises(asyncio.CancelledError):
            self.client._run(wait_until_stopped())
        self.client.close()
        self.assertTrue(self.client.loop.is_closed())

    def test_stop_before_next_operation_still_allows_cleanup(self):
        self.client.request_stop()
        with self.assertRaises(asyncio.CancelledError):
            self.client.wait(90)
        self.client.close()
        self.assertTrue(self.client.loop.is_closed())

    def test_hung_browser_operation_is_cancelled_after_timeout(self):
        async def hang():
            await asyncio.sleep(90)
        with self.assertRaises(OperationTimeout):
            self.client._run(hang(), timeout=0.1)
        self.assertIsNone(self.client._active_task)

    def test_operation_ignoring_cancellation_is_abandoned_after_grace_period(self):
        async def stubborn():
            try:
                await asyncio.sleep(90)
            except asyncio.CancelledError:
                await asyncio.sleep(0.5)
        with patch("sungnam_client.CANCEL_GRACE_SECONDS", 0.1):
            with self.assertRaises(OperationTimeout):
                self.client._run(stubborn(), timeout=0.1)

    def test_shutdown_request_still_cancels_operation_with_timeout(self):
        async def wait_until_stopped():
            self.client.loop.call_soon(self.client.request_stop)
            await asyncio.sleep(90)
        with self.assertRaises(asyncio.CancelledError):
            self.client._run(wait_until_stopped(), timeout=60)

    def test_hung_timetable_restarts_browser_for_next_scan(self):
        async def hang(*args):
            await asyncio.sleep(90)
        browser = Mock(_process=None, aclose=AsyncMock())
        self.client.browser = browser
        self.client.authenticated = True
        self.client._current_facility = "FAC26"
        self.client._get_timetable = hang
        with patch("sungnam_client.TIMETABLE_TIMEOUT", 0.1):
            with self.assertRaises(OperationTimeout):
                self.client.get_timetable("FAC26", "2026-11-10")
        browser.aclose.assert_awaited_once()
        self.assertIsNone(self.client.browser)
        self.assertFalse(self.client.authenticated)
        self.assertIsNone(self.client._current_facility)

    def test_hung_browser_is_killed_when_graceful_close_fails(self):
        async def hang(*args):
            await asyncio.sleep(90)
        process = Mock(returncode=None)
        process.wait = AsyncMock(side_effect=lambda: asyncio.sleep(90))
        self.client.browser = Mock(_process=process, send=AsyncMock(side_effect=lambda *a: asyncio.sleep(90)),
                                   aclose=AsyncMock())
        self.client._get_timetable = hang
        with patch("sungnam_client.TIMETABLE_TIMEOUT", 0.1), patch("sungnam_client.RECOVERY_TIMEOUT", 0.1):
            with self.assertRaises(OperationTimeout):
                self.client.get_timetable("FAC26", "2026-11-10")
        process.kill.assert_called()
        self.assertIsNone(self.client.browser)

    def test_manual_login_wait_has_no_overall_timeout(self):
        async def login(*args):
            return True
        self.client._login = login
        with patch.object(self.client, "_run", wraps=self.client._run) as run:
            self.assertTrue(self.client.login("", ""))
        self.assertIsNone(run.call_args.kwargs["timeout"])

    def test_manual_wait_survives_block_page_and_accepts_user_login(self):
        authenticated = {"url": "https://res.isdc.co.kr/index.do", "ready": True, "authenticated": True}
        self.client._state = AsyncMock(side_effect=[AccessBlocked("blocked"), *[authenticated] * 4])
        with patch("sungnam_client.asyncio.sleep", new_callable=AsyncMock):
            result = self.client._run(self.client._wait_state(after_login=True, manual_recovery=True))
        self.assertTrue(result["authenticated"])
        self.client.page.select.assert_not_called()

    def test_manual_wait_timeout_does_not_reload_login_page(self):
        page = self.client.page
        self.client.browser = Mock(get=AsyncMock(return_value=page), _process=None, aclose=AsyncMock())
        self.client._wait_state = AsyncMock(side_effect=ManualLoginRequired("waiting"))
        for _ in range(2):
            with self.assertRaises(ManualLoginRequired):
                self.client.login("unused", "unused")
        self.client.browser.get.assert_awaited_once()
        page.select.assert_not_called()

    def test_manual_timeout_is_reported_as_waiting_for_user(self):
        with self.assertRaises(ManualLoginRequired):
            self.client._run(self.client._wait_state(after_login=True, manual_recovery=True, timeout=0))

    def test_nodriver_remote_object_is_not_used_as_a_boolean(self):
        self.client.page.evaluate = AsyncMock(return_value=Mock(value=False))
        with self.assertRaises(SungnamError):
            self.client._run(self.client._state())

    def test_false_login_state_stays_false(self):
        self.client.page.evaluate = AsyncMock(return_value=json.dumps({
            "url": "https://res.isdc.co.kr/login.do", "ready": True,
            "login": True, "authenticated": False, "blocked": False,
        }))
        state = self.client._run(self.client._state())
        self.assertIs(state["authenticated"], False)
        with self.assertRaises(LoginFailed):
            self.client._run(self.client._wait_state(after_login=True, timeout=0.01))

    def test_blocked_login_does_not_collect_cookies_or_report_success(self):
        self.client.page.evaluate = AsyncMock(return_value=json.dumps({
            "url": "https://res.isdc.co.kr/auto_detect.do", "blocked": False,
        }))
        with self.assertRaises(AccessBlocked):
            self.client._run(self.client._state())

    def test_ui_failure_is_not_a_session_expiration(self):
        self.client._open_facility = AsyncMock(side_effect=SungnamError("unavailable"))
        with self.assertRaises(SungnamError) as result:
            self.client.get_timetable("FAC26", "2026-11-09")
        self.assertNotIsInstance(result.exception, SessionExpired)

    def test_calendar_disabled_date_is_a_valid_empty_result(self):
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(return_value=False)
        self.client._read_popup = AsyncMock()
        html = self.client.get_timetable("FAC26", "2026-11-09")
        self.assertEqual(parse_timetable(validate_timetable(200, URL, html)), ([], []))
        self.client._read_popup.assert_not_awaited()

    def test_login_waits_for_final_page_to_stabilize(self):
        self.client._state = AsyncMock(side_effect=[
            {"url": "intermediate", "ready": True, "authenticated": True},
            *[{"url": "final", "ready": True, "authenticated": True}] * 4,
        ])
        with patch("sungnam_client.asyncio.sleep", new_callable=AsyncMock):
            state = self.client._run(self.client._wait_state(after_login=True))
        self.assertEqual(state["url"], "final")
        self.assertEqual(self.client._state.call_count, 5)

    def test_timetable_view_uses_selected_facility_and_date(self):
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(return_value=True)
        self.client._read_popup = AsyncMock(return_value=TIMETABLE)
        self.assertEqual(self.client.get_timetable("FAC26", "2026-11-09"), TIMETABLE)
        self.client._open_facility.assert_awaited_once_with("FAC26")
        self.client._select_date.assert_awaited_once_with("2026-11-9")

    def test_weekend_uses_open_court_in_same_facility(self):
        self.client._facility_groups = {"FAC26": "1", "FAC41": "1", "FAC61": "5"}
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(side_effect=[False, True])
        self.client._read_popup = AsyncMock(return_value=TIMETABLE)
        self.assertEqual(self.client.get_timetable("FAC26", "2026-11-15"), TIMETABLE)
        self.assertEqual([call.args[0] for call in self.client._open_facility.await_args_list],
                         ["FAC26", "FAC41"])

    def test_court_found_for_a_weekday_kind_is_reused_for_other_dates_of_that_kind(self):
        self.client._facility_groups = {"FAC26": "1", "FAC41": "1"}
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(side_effect=[False, True, True, True])
        self.client._read_popup = AsyncMock(return_value=TIMETABLE)
        self.client.get_timetable("FAC26", "2026-11-14")
        self.client.get_timetable("FAC26", "2026-11-21")
        self.client.get_timetable("FAC26", "2026-11-12")
        self.assertEqual([call.args[0] for call in self.client._open_facility.await_args_list],
                         ["FAC26", "FAC41", "FAC41", "FAC26"])

    def test_all_disabled_courts_return_no_times_without_opening_popup(self):
        self.client._facility_groups = {"FAC26": "1", "FAC41": "1"}
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(return_value=False)
        self.client._read_popup = AsyncMock()
        html = self.client.get_timetable("FAC26", "2026-11-15")
        self.assertEqual(parse_timetable(html), ([], []))
        self.client._read_popup.assert_not_awaited()

    def test_closed_day_is_not_clickable_even_with_usedate_class(self):
        element = Mock(click=AsyncMock())
        self.client._wait_element = AsyncMock(return_value=element)
        self.client._json = AsyncMock(side_effect=[
            {"exists": True, "first": "2026-11-1"}, {"ready": True, "enabled": False, "closed": True},
        ])
        self.assertFalse(self.client._run(self.client._select_date("2026-11-9")))
        self.assertTrue(self.client._date_closed)
        element.click.assert_not_awaited()

    def test_closed_facility_does_not_scan_other_court_pages(self):
        self.client._facility_groups = {"FAC61": "5", "FAC62": "5"}
        self.client._date_closed = True
        self.client._open_facility = AsyncMock()
        self.client._select_date = AsyncMock(return_value=False)
        html = self.client.get_timetable("FAC61", "2026-11-9")
        self.assertEqual(parse_timetable(html), ([], []))
        self.client._open_facility.assert_awaited_once_with("FAC61")

    def test_date_selection_crosses_month_boundary(self):
        element = Mock(click=AsyncMock())
        self.client._wait_element = AsyncMock(return_value=element)
        self.client._json = AsyncMock(side_effect=[
            {"exists": False, "first": "2026-11-1"},
            {"ready": True, "enabled": True}, "2026-12-1",
        ])
        self.client._state = AsyncMock(return_value={"url": "https://res.isdc.co.kr/reservationInfo.do"})
        self.assertTrue(self.client._run(self.client._select_date("2026-12-1")))
        selectors = [call.args[0] for call in self.client._wait_element.await_args_list]
        self.assertIn(".ui-datepicker-next", selectors)

    def test_click_retries_with_fresh_element_when_node_is_stale(self):
        stale = Mock(click=AsyncMock(side_effect=Exception("Could not find node with given id [code: -32000]")))
        fresh = Mock(click=AsyncMock())
        self.client._wait_element = AsyncMock(return_value=fresh)
        with patch("sungnam_client.asyncio.sleep", AsyncMock()):
            self.client._run(self.client._click("#x", element=stale))
        stale.click.assert_awaited_once()
        fresh.click.assert_awaited_once()
        self.client._wait_element.assert_awaited_once_with("#x", path=None)

    def test_click_does_not_retry_other_errors(self):
        element = Mock(click=AsyncMock(side_effect=Exception("boom")))
        with self.assertRaisesRegex(Exception, "boom"):
            self.client._run(self.client._click("#x", element=element))

    def test_click_waits_before_clicking(self):
        order = []
        self.pause.side_effect = lambda: order.append("pause")
        element = Mock(click=AsyncMock(side_effect=lambda: order.append("click")))
        self.client._run(self.client._click("#x", element=element))
        self.assertEqual(order, ["pause", "click"])

    def test_page_move_waits_before_loading(self):
        order = []
        self.pause.side_effect = lambda: order.append("pause")
        self.client.browser = Mock(get=AsyncMock(side_effect=lambda url: order.append("get")),
                                   _process=None, aclose=AsyncMock())
        self.client._run(self.client._goto("https://example.test/"))
        self.assertEqual(order, ["pause", "get"])

    def test_existing_timetable_popup_is_read_and_closed(self):
        parent = self.client.page
        parent.target.target_id = "parent"
        parent.select = AsyncMock(return_value=Mock(click=AsyncMock()))
        popup = Mock(get_content=AsyncMock(return_value=TIMETABLE), close=AsyncMock())
        popup.target.target_id, popup.target.url = "popup", URL
        self.client.browser = Mock(update_targets=AsyncMock(), tabs=[parent, popup])
        self.client._state = AsyncMock(return_value={"ready": True, "url": URL})
        try:
            self.assertEqual(self.client._run(self.client._read_popup()), TIMETABLE)
            self.assertIs(self.client.page, parent)
            popup.close.assert_awaited_once()
        finally:
            self.client.browser = None

    def test_blocked_popup_is_kept_open_for_manual_recovery(self):
        parent = self.client.page
        parent.target.target_id = "parent"
        parent.select = AsyncMock(return_value=Mock(click=AsyncMock()))
        popup = Mock(close=AsyncMock())
        popup.target.target_id, popup.target.url = "popup", URL
        self.client.browser = Mock(update_targets=AsyncMock(), tabs=[parent, popup])
        self.client._state = AsyncMock(side_effect=AccessBlocked("blocked"))
        try:
            with self.assertRaises(AccessBlocked):
                self.client._run(self.client._read_popup())
            self.assertIs(self.client.page, popup)
            popup.close.assert_not_awaited()
        finally:
            self.client.browser = None


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(authenticated=False)
        self.session.login.return_value = True
        self.session.get_timetable.return_value = TIMETABLE
        self.make_session = patch.object(monitor, "sn_make_session", return_value=self.session)
        self.make_session.start()
        self.sleep = patch.object(monitor.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.make_session.stop()

    def test_four_day_scan_logs_in_once_and_closes_browser(self):
        available, courts = monitor.sn_run_once(ACCOUNTS, FACILITIES)
        self.assertEqual(len(available), 4)
        self.assertEqual(len(courts), 8)
        self.session.login.assert_called_once()
        self.session.close.assert_called_once()

    def test_each_facility_date_is_followed_by_a_random_pause(self):
        with patch.object(monitor.time, "sleep") as sleep:
            monitor.sn_run_once(ACCOUNTS, FACILITIES)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(delays), 4)
        self.assertTrue(all(1.0 <= delay <= 4.0 for delay in delays))

    def test_existing_session_is_reused_across_scans(self):
        self.session.authenticated = True
        monitor.sn_run_once(ACCOUNTS, FACILITIES, session=self.session)
        self.session.login.assert_not_called()
        self.session.close.assert_not_called()

    def test_expired_session_is_retried_once(self):
        self.session.get_timetable.side_effect = [SessionExpired(), TIMETABLE, TIMETABLE, TIMETABLE, TIMETABLE]
        monitor.sn_run_once(ACCOUNTS, FACILITIES)
        self.assertEqual(self.session.login.call_count, 2)

    def test_repeated_session_failure_aborts_instead_of_relogin_loop(self):
        self.session.get_timetable.side_effect = [SessionExpired(), TIMETABLE, SessionExpired()]
        with self.assertRaises(SessionExpired):
            monitor.sn_run_once(ACCOUNTS, FACILITIES)
        self.assertEqual(self.session.login.call_count, 2)
        self.session.close.assert_called_once()

    def test_block_does_not_try_other_accounts(self):
        self.session.login.side_effect = AccessBlocked("blocked")
        with self.assertRaises(AccessBlocked):
            monitor.sn_run_once(ACCOUNTS * 3, FACILITIES)
        self.session.login.assert_called_once()
        self.session.close.assert_called_once()

    def test_server_error_does_not_relogin(self):
        self.session.get_timetable.side_effect = SungnamError("HTTP 500")
        with self.assertRaises(SungnamError):
            monitor.sn_run_once(ACCOUNTS, FACILITIES)
        self.session.login.assert_called_once()
        self.session.close.assert_called_once()

    def test_failed_login_is_not_a_successful_empty_scan(self):
        self.session.login.side_effect = LoginFailed("bad login")
        with self.assertRaises(LoginFailed):
            monitor.sn_run_once(ACCOUNTS, FACILITIES)
        self.session.get_timetable.assert_not_called()
        self.session.close.assert_called_once()

    def test_blocked_auto_scan_preserves_results_and_retries_next_cycle(self):
        old = [{"court": "previous successful data"}]
        self.session.wait.side_effect = KeyboardInterrupt
        with patch.multiple(monitor, _sn_courts=old, _sn_available=old,
                            _sn_last_update="last-success", _sn_status="ok", _sn_error=""), \
             patch.object(monitor, "sn_run_once", side_effect=AccessBlocked("blocked")), \
             patch.object(monitor, "_notify_if_changed") as notify:
            with self.assertRaises(KeyboardInterrupt):
                monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
            data = monitor.build_sungnam_payload()
            self.assertEqual(data["all_courts"], old)
            self.assertEqual(data["last_update"], "last-success")
            self.assertEqual(data["status"], "blocked")
            self.assertEqual(data["error"], "blocked")
            self.session.wait.assert_called_once()
            notify.assert_not_called()

    def test_browser_timeout_keeps_data_and_retries_next_cycle(self):
        old = [{"court": "last successful scan"}]
        self.session.wait.side_effect = KeyboardInterrupt
        with patch.multiple(monitor, _sn_courts=old, _sn_last_update="last-success"), \
             patch.object(monitor, "sn_run_once", side_effect=OperationTimeout("hung")), \
             patch.object(monitor, "_notify_if_changed") as notify:
            with self.assertRaises(KeyboardInterrupt):
                monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
            data = monitor.build_sungnam_payload()
            self.assertEqual(data["status"], "error")
            self.assertEqual(data["all_courts"], old)
            self.assertEqual(data["last_update"], "last-success")
            self.session.wait.assert_called_once()
            notify.assert_not_called()

    def test_login_failure_stops_reconnecting(self):
        with patch.object(monitor, "sn_run_once", side_effect=LoginFailed("bad credentials")):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
        self.session.wait.assert_not_called()

    def test_manual_login_does_not_cycle_through_saved_accounts(self):
        self.session.manual_login = True
        self.session.login.side_effect = ManualLoginRequired("waiting")
        with self.assertRaises(ManualLoginRequired):
            monitor.sn_run_once(ACCOUNTS * 3, FACILITIES)
        self.session.login.assert_called_once_with("", "")
        self.session.get_timetable.assert_not_called()

    def test_browser_start_failure_does_not_retry_or_overwrite_data(self):
        old = [{"court": "last successful scan"}]
        with patch.multiple(monitor, _sn_courts=old, _sn_last_update="last-success"), \
             patch.object(monitor, "sn_run_once", side_effect=BrowserLaunchError("profile busy")):
            monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
            self.assertEqual(monitor._sn_courts, old)
            self.assertEqual(monitor._sn_last_update, "last-success")
            self.session.wait.assert_not_called()

    def test_program_shutdown_requests_browser_cleanup_and_waits_for_thread(self):
        thread = Mock(is_alive=Mock(return_value=False))
        with patch.object(monitor, "_sn_client", self.session), \
             patch.object(monitor, "_sn_stop_event", threading.Event()) as stop_event:
            monitor.stop_sungnam(thread)
            self.assertTrue(stop_event.is_set())
        self.session.request_stop.assert_called_once()
        thread.join.assert_called_once_with(timeout=10)

    def test_manual_wait_preserves_data_and_dashboard_shows_login_pending(self):
        self.session.manual_login = True
        self.session.wait.side_effect = KeyboardInterrupt
        old = [{"court": "last successful scan"}]
        with patch.multiple(monitor, _sn_courts=old, _sn_available=old,
                            _sn_last_update="last-success", _sn_status="ok", _sn_error=""), \
             patch.object(monitor, "sn_run_once", side_effect=ManualLoginRequired("waiting")), \
             patch.object(monitor, "_notify_if_changed") as notify:
            with self.assertRaises(KeyboardInterrupt):
                monitor._sungnam_monitor_loop(self.session, ACCOUNTS, FACILITIES, FACILITIES)
            data = monitor.build_sungnam_payload()
            self.assertEqual(data["status"], "waiting_login")
            self.assertEqual(data["all_courts"], old)
            self.assertEqual(data["last_update"], "last-success")
            self.session.wait.assert_called_once_with(5)
            notify.assert_not_called()


class HumanPauseTests(unittest.TestCase):
    def test_pause_is_random_between_one_and_four_seconds(self):
        with patch("sungnam_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            for _ in range(200):
                asyncio.run(_human_pause())
        delays = [call.args[0] for call in sleep.await_args_list]
        self.assertTrue(all(1.0 <= delay <= 4.0 for delay in delays))
        self.assertGreater(len(set(delays)), 1)


class CommandLineTests(unittest.TestCase):
    def test_poll_delay_is_random_between_two_and_five_minutes(self):
        with patch.object(monitor.random, "randint", return_value=200) as randint:
            self.assertEqual(monitor.next_poll_delay(), 200)
        randint.assert_called_once_with(120, 300)
        delays = {monitor.next_poll_delay() for _ in range(200)}
        self.assertTrue(all(120 <= delay <= 300 for delay in delays))
        self.assertGreater(len(delays), 1)

    def test_sungnam_waits_a_random_delay_between_cycles(self):
        session = Mock(authenticated=False, manual_login=False)
        session.wait.side_effect = KeyboardInterrupt
        with patch.object(monitor, "next_poll_delay", return_value=173), \
             patch.object(monitor, "sn_run_once", return_value=([], [])), \
             patch.object(monitor, "_sn_begin_cycle"):
            with self.assertRaises(KeyboardInterrupt):
                monitor._sungnam_monitor_loop(session, ACCOUNTS, FACILITIES, [])
        session.wait.assert_called_once_with(173)

    def test_default_command_uses_auto_login(self):
        self.assertFalse(monitor.create_argument_parser().parse_args([]).sn_manual_login)

    def test_old_manual_option_still_works(self):
        self.assertTrue(monitor.create_argument_parser().parse_args(["--sn-manual-login"]).sn_manual_login)

    def test_explicit_auto_option_still_works(self):
        self.assertFalse(monitor.create_argument_parser().parse_args(["--sn-auto-login"]).sn_manual_login)

    def test_supported_platform_default_is_background(self):
        for platform, expected in (("win32", True), ("linux", True), ("darwin", False)):
            with self.subTest(platform=platform), patch("browser_background.sys.platform", platform):
                self.assertEqual(monitor.create_argument_parser().parse_args([]).sn_background, expected)

    def test_linux_browser_and_profile_overrides(self):
        args = monitor.create_argument_parser().parse_args([
            "--sn-browser", "/usr/bin/chromium", "--sn-profile", "/tmp/tennis-profile",
        ])
        self.assertEqual(args.sn_browser, "/usr/bin/chromium")
        self.assertEqual(args.sn_profile, "/tmp/tennis-profile")

    def test_show_browser_option_enables_visible_mode(self):
        self.assertFalse(monitor.create_argument_parser().parse_args(["--sn-show-browser"]).sn_background)


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        display = patch.dict(os.environ, {"DISPLAY": ":42"})
        display.start()
        self.addCleanup(display.stop)
        pause = patch("sungnam_client._human_pause", new_callable=AsyncMock)
        self.pause = pause.start()
        self.addCleanup(pause.stop)
        self.client = SungnamClient("unused-auto-test-profile", background=False)
        self.id_field = Mock(clear_input=AsyncMock(), send_keys=AsyncMock())
        self.password_field = Mock(clear_input=AsyncMock(), send_keys=AsyncMock())
        self.button = Mock(click=AsyncMock())
        self.page = Mock(select=AsyncMock(side_effect=[self.id_field, self.password_field, self.button]))
        self.browser = Mock(get=AsyncMock(return_value=self.page), start=AsyncMock(),
                            _process=None, aclose=AsyncMock())
        self.client.browser = self.browser
        self.client._wait_state = AsyncMock(side_effect=[{"login": True}, {"authenticated": True}])

    def tearDown(self):
        self.client.close()

    def test_default_auto_login_submits_saved_credentials(self):
        self.assertFalse(self.client.manual_login)
        self.assertTrue(self.client.login("saved-user", "saved-password"))
        self.id_field.send_keys.assert_awaited_once_with("saved-user")
        self.password_field.send_keys.assert_awaited_once_with("saved-password")
        self.button.click.assert_awaited_once()
        self.assertTrue(self.client.authenticated)

    def test_login_pauses_before_page_load_each_field_and_button(self):
        self.client.login("saved-user", "saved-password")
        self.assertEqual(self.pause.await_count, 4)

    def test_existing_session_does_not_resubmit_credentials(self):
        self.client._wait_state = AsyncMock(return_value={"authenticated": True})
        self.assertTrue(self.client.login("saved-user", "saved-password"))
        self.page.select.assert_not_awaited()

    def test_auto_login_uses_gui_chrome_engine(self):
        self.client.browser = None
        with patch("sungnam_client.uc.Config") as config, \
             patch("sungnam_client.uc.Browser", return_value=self.browser):
            self.assertTrue(self.client.login("saved-user", "saved-password"))
        self.assertIs(config.call_args.kwargs["headless"], False)


class BackgroundBrowserTests(unittest.TestCase):
    def setUp(self):
        self.client = SungnamClient("unused-background-profile", background=True)
        self.desktop = Mock(aclose=AsyncMock())
        self.process = Mock(pid=12345, returncode=0, _transport=Mock())
        self.desktop.start_process_async = AsyncMock(return_value=self.process)
        self.browser = Mock(_process=None, start=AsyncMock(), aclose=AsyncMock())
        self.browser.main_tab.activate = AsyncMock()
        self.config = Mock(browser_executable_path="chrome.exe", return_value=["--window-size=1920,1080"])

    def tearDown(self):
        self.client.close()

    def launch(self):
        with patch("sungnam_client.create_background_desktop", return_value=self.desktop), \
             patch("sungnam_client._wait_for_browser_port", new_callable=AsyncMock), \
             patch("sungnam_client.uc.Config", return_value=self.config) as config, \
             patch("sungnam_client.uc.Browser", return_value=self.browser):
            self.client._run(self.client._start_browser())
            return config.call_args, self.desktop.start_process_async.call_args

    def test_background_launch_assigns_private_desktop_and_owns_process(self):
        config_args, spawn_args = self.launch()
        self.assertIs(config_args.kwargs["headless"], False)
        self.assertEqual(spawn_args.args[0], "chrome.exe")
        self.assertEqual(spawn_args.args[1], ["--window-size=1920,1080"])
        self.assertIs(self.client.browser._process, self.process)
        self.assertEqual(self.client.browser._process_pid, 12345)
        self.client.close()
        self.desktop.aclose.assert_awaited_once()

    def test_attach_failure_closes_process_transport_and_desktop(self):
        self.process.stderr = Mock(read=AsyncMock(return_value=b"startup failed"))
        self.browser.start.side_effect = RuntimeError("cannot connect")
        with self.assertRaises(BrowserLaunchError):
            self.launch()
        self.process._transport.close.assert_called_once()
        self.desktop.aclose.assert_awaited_once()
        self.assertIsNone(self.client.browser)
        self.assertIsNone(self.client._desktop)

    def test_desktop_failure_does_not_fall_back_to_visible_window(self):
        with patch("sungnam_client.create_background_desktop", side_effect=OSError("denied")), \
             patch("sungnam_client.uc.Config", return_value=self.config), \
             patch("sungnam_client.uc.Browser", return_value=self.browser), \
             patch("sungnam_client.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            with self.assertRaisesRegex(BrowserLaunchError, "--sn-show-browser"):
                self.client._run(self.client._start_browser())
            spawn.assert_not_awaited()

    def test_manual_login_always_uses_visible_desktop(self):
        manual = SungnamClient("unused-manual-profile", manual_login=True, background=True)
        try:
            self.assertFalse(manual.background)
        finally:
            manual.close()


if __name__ == "__main__":
    unittest.main()
