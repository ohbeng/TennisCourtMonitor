"""성남 예약 사이트의 로그인과 시간표 조회를 같은 Chrome 세션에서 수행한다."""

import asyncio
import json
import logging
import os
import random
import re
import sys
from urllib.parse import urlsplit

import nodriver as uc
from bs4 import BeautifulSoup
from browser_background import create_background_desktop, supports_background


BASE_URL = "https://res.isdc.co.kr"

# Chrome DevTools 명령 중 일부는 응답이 오지 않으면 영원히 기다린다. 한 번의 브라우저
# 작업(로그인, 시간표 1건)에 전체 시간 제한을 둬서 조회 루프가 멈추지 않게 한다.
LOGIN_TIMEOUT = 180
TIMETABLE_TIMEOUT = 300
CANCEL_GRACE_SECONDS = 10
RECOVERY_TIMEOUT = 20
# 자동화로 탐지되지 않도록 클릭·페이지 이동·입력 사이에 쉬는 무작위 시간(초).
HUMAN_DELAY_RANGE = (1.0, 4.0)


async def _human_pause():
    await asyncio.sleep(random.uniform(*HUMAN_DELAY_RANGE))


class SungnamError(RuntimeError):
    pass


class OperationTimeout(SungnamError):
    pass


class BrowserLaunchError(SungnamError):
    pass


class AccessBlocked(SungnamError):
    pass


class LoginFailed(SungnamError):
    pass


class ManualLoginRequired(LoginFailed):
    pass


class SessionExpired(SungnamError):
    pass


async def _wait_for_browser_port(host, port, process, timeout=20):
    # Xvfb 준비와 첫 Chrome 실행이 nodriver의 짧은 연결 재시도보다 느릴 수 있다.
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise BrowserLaunchError(f"Chrome 시작 중 종료되었습니다 (종료 코드: {process.returncode}).")
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.5)
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.2)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise BrowserLaunchError(f"Chrome DevTools 연결 준비가 {timeout}초 안에 완료되지 않았습니다.")


def validate_timetable(status, url, html):
    """빈 예약 결과와 인증/차단/HTTP 오류를 구분한다. 오류를 빈 목록으로 바꾸지 않는다."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style"):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    path = urlsplit(url).path.lower()
    if "auto_detect.do" in path or any(s in text for s in (
        "비정상 접근 탐지", "비정상적인 접근이 탐지되었습니다",
    )):
        raise AccessBlocked("성남 사이트가 접근을 차단했습니다. 자동 재로그인을 중단합니다.")
    if status in (403, 429):
        raise AccessBlocked(f"성남 사이트가 요청을 제한했습니다 (HTTP {status}).")
    if status == 401 or "login.do" in path or soup.select_one("#web_id, #loginForm"):
        raise SessionExpired("성남 로그인 세션이 만료되었습니다.")
    if html.strip().lower() == "session" or re.search(
        r"(?:location(?:\.href)?\s*=|location\.replace\()\s*['\"][^'\"]*login\.do", html,
    ):
        raise SessionExpired("성남 로그인 세션이 만료되었습니다.")
    if status != 200:
        raise SungnamError(f"성남 시간표 HTTP 오류: {status}")
    if soup.select_one(".lb-timetable") or "이용가능한 시간이 없습니다" in text:
        return html
    raise SungnamError("성남 시간표 응답 형식을 확인할 수 없습니다. 빈 코트로 처리하지 않습니다.")


def parse_timetable(html):
    """따옴표, class 순서, 태그 안의 줄바꿈과 관계없이 시간표를 읽는다."""
    soup = BeautifulSoup(html, "html.parser")
    available, all_slots = [], []
    for label in soup.select(".lb-timetable"):
        court = re.search(r"(\d+)\s*번\s*코트", label.get_text(" ", strip=True))
        box = label.find_next("div", class_="tableBox")
        if not court or box is None:
            raise SungnamError("성남 코트 시간표 구조가 변경되었습니다.")
        if not box.select("tbody tr") and "이용가능한 시간이 없습니다" not in box.get_text():
            raise SungnamError("성남 코트 시간표에 행이 없습니다.")
        for row in box.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            values = [cell.get_text(" ", strip=True) for cell in cells]
            time_index, times = None, None
            for index, value in enumerate(values):
                times = re.search(r"(\d{1,2}:\d{2})\s*[~～]\s*(\d{1,2}:\d{2})", value)
                if times:
                    time_index = index
                    break
            if times is None:
                if row.find("td") and not any("이용가능한 시간이 없습니다" in v for v in values):
                    raise SungnamError("성남 시간표 행을 해석할 수 없습니다.")
                continue
            start, end = (t.zfill(5) for t in times.groups())
            slot = {
                "court": f"{court.group(1)}번 코트",
                "time": f"{start} ~ {end}",
                "is_available": "예약가능" in re.sub(r"\s+", "", values[0]),
                "reservation_name": " ".join(values[time_index + 1:]),
            }
            all_slots.append(slot)
            if slot["is_available"]:
                available.append(slot)
    return available, all_slots


class SungnamClient:
    def __init__(self, profile, manual_login=False, background=None, browser_executable=None):
        self.profile = profile
        self.manual_login = manual_login
        self.background = (supports_background() if background is None else background) and not manual_login
        self.browser_executable = browser_executable or os.environ.get("CHROME_BIN")
        self._desktop = None
        self.browser = None
        self.page = None
        self.loop = asyncio.new_event_loop()
        self.login_dialog = False
        self.authenticated = False
        self._manual_waiting = False
        self._stop_requested = False
        self._active_task = None

    def _run(self, coroutine, *, closing=False, timeout=None):
        if self._stop_requested and not closing:
            coroutine.close()
            raise asyncio.CancelledError()
        asyncio.set_event_loop(self.loop)
        task = self.loop.create_task(coroutine)
        self._active_task = task
        try:
            if timeout is None:
                return self.loop.run_until_complete(task)
            done, _ = self.loop.run_until_complete(asyncio.wait({task}, timeout=timeout))
            if done:
                return task.result()
            # 취소 정리 중에도 멈출 수 있으므로 유예 시간 뒤에는 작업을 포기한다.
            task.cancel()
            done, _ = self.loop.run_until_complete(asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS))
            if done and not task.cancelled():
                task.exception()
            raise OperationTimeout(f"성남 브라우저 작업이 {timeout}초 안에 끝나지 않아 중단했습니다.")
        finally:
            self._active_task = None

    def request_stop(self):
        """메인 스레드의 Ctrl+C를 브라우저를 소유한 스레드에 전달한다."""
        self._stop_requested = True
        task = self._active_task
        if task is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(task.cancel)

    async def _start_browser(self):
        # create()가 실패하면 호출자에게 Browser를 반환하지 않아 실패한 Chrome이
        # 남을 수 있다. 시작 전부터 객체를 보관해 성공/실패 모두 직접 정리한다.
        try:
            if (sys.platform.startswith("linux") and not self.background
                    and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
                raise BrowserLaunchError(
                    "Linux 화면 표시/직접 로그인에는 데스크톱 세션의 DISPLAY 또는 WAYLAND_DISPLAY가 필요합니다. "
                    "화면 없는 서버에서는 직접 로그인/화면 표시 옵션을 빼고 --sn-background로 자동 로그인하세요."
                )
            browser_args = ["--window-size=1920,1080"]
            if sys.platform.startswith("linux"):
                # 메모리가 작은 서버용: GPU/확장/백그라운드 프로세스와 렌더러 수를 줄인다.
                browser_args += [
                    "--disable-gpu", "--disable-extensions", "--disable-background-networking",
                    "--disable-component-update", "--disable-sync", "--mute-audio",
                    "--renderer-process-limit=1", "--js-flags=--max-old-space-size=192",
                ]
            try:
                config = uc.Config(
                    headless=False, lang="ko-KR", user_data_dir=self.profile,
                    browser_executable_path=(os.path.expanduser(self.browser_executable)
                                             if self.browser_executable else None),
                    sandbox=True, browser_args=browser_args,
                )
            except FileNotFoundError as exc:
                raise BrowserLaunchError(
                    "Chrome/Chromium을 찾을 수 없습니다. 브라우저를 설치한 뒤 "
                    "--sn-browser /브라우저/경로 또는 CHROME_BIN 환경변수로 지정하세요."
                ) from exc
            self.browser = uc.Browser(config)
            if self.background:
                try:
                    self._desktop = create_background_desktop()
                except OSError as exc:
                    raise BrowserLaunchError(
                        f"Chrome 백그라운드 작업 공간 생성 실패: {exc}. "
                        "데스크톱 세션에서는 --sn-show-browser로 실행할 수 있습니다."
                    ) from exc
                config.host, config.port = "127.0.0.1", uc.core.util.free_port()
                process = await self._desktop.start_process_async(config.browser_executable_path, config())
                # host/port가 지정된 nodriver는 이미 시작한 Chrome에 연결한다.
                # 실패·취소 시에도 process를 보관해 전용 Chrome을 정리한다.
                try:
                    await _wait_for_browser_port(config.host, config.port, process)
                    await self.browser.start()
                finally:
                    self.browser._process, self.browser._process_pid = process, process.pid
                await self.browser.main_tab.activate()
                logging.info("[SN] Chrome 백그라운드 실행 시작 (PID=%s)", process.pid)
            else:
                await self.browser.start()
        except BaseException as exc:
            browser = self.browser
            process = browser._process if browser is not None else None
            exit_code = process.returncode if process is not None else None
            details = ""
            if process is not None and exit_code is not None and process.stderr is not None:
                try:
                    raw = await asyncio.wait_for(process.stderr.read(2048), timeout=1)
                    details = re.sub(r"\s+", " ", raw.decode("utf-8", errors="replace")).strip()
                except (Exception, asyncio.CancelledError):
                    pass
            await self._close()
            if isinstance(exc, (BrowserLaunchError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            message = (
                f"Chrome 연결 실패 (프로필: {self.profile}, 종료 코드: {exit_code}). "
                "같은 프로필의 이전 모니터용 Chrome이 남아 있거나 Chrome 시작에 실패했습니다. "
                "이전 모니터를 Ctrl+C로 종료한 뒤 다시 실행하세요. "
                "화면 확인은 --sn-show-browser 옵션을 사용하세요."
            )
            if details:
                logging.error("[SN] Chrome 시작 오류: %s", details)
            raise BrowserLaunchError(message) from exc

    async def _json(self, expression, *, await_promise=False):
        # nodriver 0.50은 false/null을 RemoteObject로 반환할 수 있다.
        # JSON 문자열만 수락하여 객체의 truthiness를 인증 판정에 사용하지 않는다.
        raw = await asyncio.wait_for(
            self.page.evaluate(expression, await_promise=await_promise, return_by_value=True),
            timeout=10,
        )
        if not isinstance(raw, str):
            raise SungnamError("성남 브라우저 응답을 읽지 못했습니다.")
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise SungnamError("성남 브라우저 응답이 올바른 JSON이 아닙니다.") from exc

    async def _state(self):
        state = await self._json("""JSON.stringify({
            url: location.href,
            ready: document.readyState === 'complete',
            login: !!document.querySelector('#web_id') && !!document.querySelector('#web_pw'),
            authenticated: Array.from(document.querySelectorAll('a,button')).some(el =>
                /logout\\.do/i.test(el.getAttribute('href') || '') ||
                el.textContent.trim() === '로그아웃'),
            blocked: /비정상 접근 탐지|비정상적인 접근이 탐지되었습니다/.test(
                document.body ? document.body.innerText : '')
        })""")
        if not isinstance(state, dict):
            raise SungnamError("성남 페이지 상태를 확인할 수 없습니다.")
        if "auto_detect.do" in urlsplit(state.get("url", "")).path or state.get("blocked") is True:
            raise AccessBlocked(
                f"성남 사이트가 접근을 차단했습니다 (URL: {state.get('url', '')}). " + (
                    "화면 확인이 필요하면 --sn-show-browser로 실행하세요." if self.background else
                    "열린 Chrome에서 사이트 안내를 확인해 주세요."
                )
            )
        return state

    async def _wait_state(self, *, after_login=False, timeout=30, manual_recovery=False):
        deadline = self.loop.time() + timeout
        stable_url, stable_count = None, 0
        blocked_notice = False
        while self.loop.time() < deadline:
            try:
                state = await self._state()
            except AccessBlocked:
                if not manual_recovery:
                    raise
                if not blocked_notice:
                    logging.warning("[SN] 차단 화면입니다. 자동 접속을 멈추고 열린 Chrome에서 사용자 조치를 기다립니다.")
                    blocked_notice = True
                stable_url, stable_count = None, 0
                await asyncio.sleep(0.5)
                continue
            except SungnamError:
                # 페이지 전환 중에는 JS 실행 컨텍스트가 잠시 사라질 수 있다.
                await asyncio.sleep(0.5)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if state.get("ready") is True:
                if state.get("authenticated") is True:
                    if state.get("url") == stable_url:
                        stable_count += 1
                    else:
                        stable_url, stable_count = state.get("url"), 1
                    if stable_count >= 4:
                        return state
                else:
                    stable_url, stable_count = None, 0
                if not after_login and state.get("login") is True:
                    return state
            if after_login and self.login_dialog:
                raise LoginFailed("성남 로그인이 거절되었습니다. 계정 정보와 사이트 안내를 확인하세요.")
            await asyncio.sleep(0.5)
        if manual_recovery:
            raise ManualLoginRequired("열린 Chrome에서 직접 로그인해 주세요. 페이지를 다시 요청하지 않고 기다립니다.")
        raise LoginFailed("성남 로그인 확인 시간이 초과되었습니다. 로그인 성공으로 처리하지 않습니다.")

    async def _dialog(self, event):
        self.login_dialog = True
        await self.page.send(uc.cdp.page.handle_javascript_dialog(accept=True))

    async def _login(self, username, password):
        if self.browser is None:
            await self._start_browser()
        if self.manual_login:
            # 로그인 대기가 끝나도 같은 페이지를 유지한다. 차단 페이지에서
            # 주기적으로 login.do를 다시 요청하거나 비밀번호를 제출하지 않는다.
            if not self._manual_waiting or self.page is None:
                self.page = await self._goto(f"{BASE_URL}/login.do")
            self._manual_waiting = True
            logging.info("[SN] Chrome에서 직접 로그인하세요. 기존 로그인 세션이 있으면 그대로 사용합니다.")
            await self._wait_state(after_login=True, timeout=180, manual_recovery=True)
            self._manual_waiting = False
            logging.info("[SN] 로그인 확인 완료 (직접 로그인/기존 세션)")
            return True
        self.page = await self._goto(f"{BASE_URL}/login.do")
        state = await self._wait_state()
        if state.get("authenticated") is True:
            logging.info("[SN] 기존 브라우저 로그인 세션 확인")
            return True
        self.login_dialog = False
        self.page.add_handler(uc.cdp.page.JavascriptDialogOpening, self._dialog)
        logging.info("[SN] 저장된 계정으로 자동 로그인합니다 (%s).", "백그라운드" if self.background else "화면 표시")
        for selector, value in (("#web_id", username), ("#web_pw", password)):
            await _human_pause()
            element = await self.page.select(selector, timeout=10)
            if element is None:
                raise LoginFailed("성남 로그인 입력란을 찾지 못했습니다.")
            await element.clear_input()
            await element.send_keys(value)
        await _human_pause()
        button = await self.page.select("#btn_login", timeout=10)
        if button is None:
            raise LoginFailed("성남 로그인 버튼을 찾지 못했습니다.")
        await button.click()
        await self._wait_state(after_login=True, timeout=30)
        logging.info("[SN] 로그인 확인 완료 (로그아웃 메뉴 확인)")
        return True

    def login(self, username, password):
        self.authenticated = False
        self._current_facility = None
        try:
            # 직접 로그인은 사용자가 조치할 때까지 기다리므로 전체 시간 제한을 두지 않는다.
            self.authenticated = self._run(
                self._login(username, password), timeout=None if self.manual_login else LOGIN_TIMEOUT,
            ) is True
        except OperationTimeout:
            self._recover_after_timeout()
            raise
        return self.authenticated

    async def _wait_element(self, selector, *, path=None, timeout=30):
        deadline = self.loop.time() + timeout
        last_url = None
        while self.loop.time() < deadline:
            try:
                state = await self._state()
                last_url = state.get("url")
                if state.get("login") is True:
                    raise SessionExpired("성남 로그인 세션이 만료되었습니다.")
                if state.get("ready") is True and (path is None or path in state.get("url", "")):
                    element = await self.page.select(selector, timeout=0.5)
                    if element is not None:
                        return element
            except (AccessBlocked, SessionExpired):
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)
        detail = f"선택자: {selector}, 기대 경로: {path}, 현재 URL: {last_url}"
        try:
            body = await self._json("JSON.stringify((document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 200))")
            detail += f", 화면: {body}"
        except Exception:
            pass
        raise SungnamError(f"성남 조회 화면을 기다리는 시간이 초과되었습니다 ({detail}).")

    async def _goto(self, url):
        await _human_pause()
        return await self.browser.get(url)

    async def _click(self, selector, *, path=None, element=None):
        await _human_pause()
        # 페이지가 다시 그려지면 이미 찾은 요소의 DOM 노드가 사라져 click이 실패한다.
        # 이 경우 요소를 다시 찾아 클릭한다.
        for attempt in range(5):
            if element is None:
                element = await self._wait_element(selector, path=path)
            try:
                await element.click()
                return
            except Exception as exc:
                if "Could not find node" not in str(exc) or attempt == 4:
                    raise
                element = None
                await asyncio.sleep(0.5)

    async def _open_facility(self, facility_id):
        if getattr(self, "_current_facility", None) == facility_id:
            return
        # 일반 사용자의 시설 목록 → 코트 → 날짜 선택 순서를 따른다.
        # otherTimetable.do는 AJAX API가 아니라 POST로 여는 별도 화면이다.
        group_map = getattr(self, "_facility_groups", {})
        preferred = group_map.get(facility_id) or {
            "FAC26": "1", "FAC61": "5", "FAC99": "14", "FAC101": "15",
        }.get(facility_id)
        self.page = await self._goto(f"{BASE_URL}/facilityList.do?facType=29")
        await self._wait_element(".tennisGroup", path="facilityList.do")
        groups = await self._json("JSON.stringify(Array.from(document.querySelectorAll('.tennisGroup')).map(x=>x.id))")
        if preferred in groups:
            groups = [preferred] + [group for group in groups if group != preferred]
        for index, group in enumerate(groups):
            if index:
                self.page = await self._goto(f"{BASE_URL}/facilityList.do?facType=29")
                await self._wait_element(".tennisGroup", path="facilityList.do")
            await self._click(f'.tennisGroup[id={json.dumps(group)}]', path="facilityList.do")
            await self._wait_element(".facilityInfo", path="tennisList.do")
            ids = await self._json("JSON.stringify(Array.from(document.querySelectorAll('.facilityInfo')).map(x=>x.id))")
            group_map.update({fac_id: group for fac_id in ids})
            self._facility_groups = group_map
            if facility_id not in ids:
                continue
            await self._click(f'.facilityInfo[id={json.dumps(facility_id)}]', path="tennisList.do")
            await self._wait_element("#move_reservation", path="facilityInfo.do")
            self._current_facility = facility_id
            return
        raise SungnamError(f"성남 시설 목록에서 {facility_id}를 찾지 못했습니다.")

    async def _select_date(self, date):
        self._date_closed = False
        await self._wait_element(".spandate")
        # 조회 범위가 월말을 넘으면 사이트 달력의 이전/다음 달 버튼을 사용한다.
        calendar = await self._json("JSON.stringify({exists:!!document.getElementById(%s), first:Array.from(document.querySelectorAll('.spandate[id]')).map(x=>x.id).find(x=>/^\\d{4}-\\d{1,2}-\\d{1,2}$/.test(x))})" % json.dumps(date))
        if not calendar.get("exists") and calendar.get("first"):
            wanted = tuple(map(int, date.split("-")[:2]))
            current = tuple(map(int, calendar["first"].split("-")[:2]))
            control = ".ui-datepicker-next" if wanted > current else ".ui-datepicker-prev"
            await self._click(control)
        element = await self._wait_element(f'.spandate[id={json.dumps(date)}]')
        deadline = self.loop.time() + 20
        while self.loop.time() < deadline:
            calendar = await self._json("""JSON.stringify((() => {
                const el = document.getElementById(%s);
                const events = el && window.jQuery && jQuery._data(el, 'events');
                const closed = !!el && el.classList.contains('closed');
                return {
                    ready: (!!(events && events.click && events.click.length) || closed) && jQuery.active === 0,
                    closed: closed,
                    enabled: !!el && el.classList.contains('usedate') &&
                        !el.classList.contains('closed') && !el.classList.contains('notusedate')
                };
            })())""" % json.dumps(date))
            if calendar.get("ready") is True:
                break
            await asyncio.sleep(0.5)
        else:
            raise SungnamError("성남 달력 초기화가 완료되지 않았습니다.")
        self._date_closed = calendar.get("closed") is True
        if calendar.get("enabled") is not True:
            return False
        await self._click(f'.spandate[id={json.dumps(date)}]', element=element)
        selected = await self._json("JSON.stringify(document.querySelector('#resdate')?.value)")
        if selected != date:
            raise SungnamError("성남 달력의 날짜 선택을 확인하지 못했습니다.")
        # 시설 소개 화면에서만 날짜 선택 후 시간표 화면으로 이동한다.
        state = await self._state()
        if "facilityInfo.do" in state.get("url", ""):
            await self._click("#move_reservation")
        await self._wait_element("#other_timetable", path="reservationInfo.do")
        return True

    async def _read_popup(self):
        parent = self.page
        await self.browser.update_targets()
        previous = {tab.target.target_id for tab in self.browser.tabs}
        button = await self.page.select("#other_timetable")
        await self._click("#other_timetable", element=button)
        popup = None
        keep_popup = False
        deadline = self.loop.time() + 30
        try:
            while self.loop.time() < deadline:
                await self.browser.update_targets()
                popup = next((tab for tab in self.browser.tabs if tab is not parent and (
                    tab.target.target_id not in previous or "otherTimetable.do" in tab.target.url
                )), None)
                if popup is not None:
                    break
                await asyncio.sleep(0.5)
            if popup is None:
                raise SungnamError("성남 다른 코트 시간표 창을 열지 못했습니다.")
            self.page = popup
            while self.loop.time() < deadline:
                try:
                    state = await self._state()
                    if state.get("login") is True:
                        raise SessionExpired("성남 시간표 로그인 세션이 만료되었습니다.")
                    if state.get("ready") is True:
                        html = await popup.get_content()
                        try:
                            return validate_timetable(200, state.get("url", ""), html)
                        except (AccessBlocked, SessionExpired):
                            raise
                        except SungnamError:
                            # 보안 스크립트의 중간 로딩 화면은 실제 시간표가 아니다.
                            pass
                except (AccessBlocked, SessionExpired):
                    raise
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            raise SungnamError("성남 시간표 화면의 로딩 시간이 초과되었습니다.")
        except AccessBlocked:
            keep_popup = self.manual_login and popup is not None
            raise
        finally:
            self.page = popup if keep_popup else parent
            if popup is not None and not keep_popup:
                await popup.close()

    async def _get_timetable(self, facility_id, date_str):
        if self.page is None:
            raise SessionExpired("성남 로그인이 필요합니다.")
        date = "-".join(str(int(part)) for part in date_str.split("-"))
        representatives = getattr(self, "_date_facilities", {})
        first = representatives.get((facility_id, date), facility_id)
        await self._open_facility(first)
        if await self._select_date(date):
            return await self._read_popup()
        if getattr(self, "_date_closed", False):
            return "<p>이용가능한 시간이 없습니다</p>"
        # 탄천 등은 평일/토요일/일요일 코트 코드가 따로 있다.
        # 지정 코트의 달력이 닫혀 있어도 같은 시설의 다른 코트 달력을 확인한다.
        group_map = getattr(self, "_facility_groups", {})
        group = group_map.get(facility_id)
        siblings = [fac_id for fac_id, group_id in group_map.items()
                    if group is not None and group_id == group and fac_id != first]
        for candidate in siblings:
            await self._open_facility(candidate)
            if await self._select_date(date):
                representatives[facility_id, date] = candidate
                self._date_facilities = representatives
                return await self._read_popup()
        return "<p>이용가능한 시간이 없습니다</p>"

    def get_timetable(self, facility_id, date_str):
        try:
            return self._run(self._get_timetable(facility_id, date_str), timeout=TIMETABLE_TIMEOUT)
        except OperationTimeout:
            self._recover_after_timeout()
            raise
        except (AccessBlocked, SessionExpired) as exc:
            self.authenticated = False
            self._current_facility = None
            self._manual_waiting = self.manual_login and isinstance(exc, AccessBlocked)
            raise

    def _recover_after_timeout(self):
        """응답 없는 Chrome을 버린다. 다음 조회가 새 Chrome을 시작하고 다시 로그인한다."""
        logging.error("[SN] 브라우저 작업이 제한 시간을 넘겨 Chrome을 종료하고 다음 조회에서 다시 시작합니다.")
        browser = self.browser
        process = browser._process if browser is not None else None
        try:
            self._run(self._close(), closing=True, timeout=RECOVERY_TIMEOUT)
        except Exception as exc:
            logging.warning("[SN] Chrome 정상 종료에 실패해 강제 종료합니다: %s", exc)
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
        if self._desktop is not None:
            try:
                self._run(self._desktop.aclose(), closing=True, timeout=RECOVERY_TIMEOUT)
            except Exception as exc:
                logging.warning("[SN] 백그라운드 작업 공간 정리 실패: %s", exc)
        self.browser = self.page = self._desktop = None
        self.authenticated = False
        self._current_facility = None
        self._manual_waiting = False

    def wait(self, seconds):
        # 폴링 사이에도 DevTools 연결과 브라우저 이벤트를 처리한다.
        self._run(asyncio.sleep(seconds))

    async def _close(self):
        browser = self.browser
        if browser is None:
            return
        process = browser._process
        try:
            if process is not None and process.returncode is None:
                try:
                    await asyncio.wait_for(browser.send(uc.cdp.browser.close()), timeout=2)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    browser.stop()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=2)
            if process is not None:
                transport = getattr(process, "_transport", None)
                if transport is not None:
                    transport.close()
            await browser.aclose()
            await asyncio.sleep(0.1)
        finally:
            uc.core.util.get_registered_instances().discard(browser)
            self.browser = None
            self.page = None
            self.authenticated = False
            self._manual_waiting = False
            if self._desktop is not None:
                try:
                    await self._desktop.aclose()
                except OSError as exc:
                    logging.warning("[SN] 백그라운드 작업 공간 정리 실패: %s", exc)
                self._desktop = None

    def close(self):
        if self.loop.is_closed():
            return
        try:
            self._run(self._close(), closing=True)
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                # 취소에 응답하지 않는 작업 때문에 종료가 멈추지 않게 한다.
                self.loop.run_until_complete(asyncio.wait(pending, timeout=CANCEL_GRACE_SECONDS))
                for task in pending:
                    if task.done() and not task.cancelled():
                        task.exception()
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            asyncio.set_event_loop(None)
