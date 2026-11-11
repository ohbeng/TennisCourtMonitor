#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
성남 + 용인 테니스 코트 예약 현황 모니터 (Linux 서비스용)
  python tennis_court_monitor_all.py [--webpage-url https://...onrender.com]

수집한 결과는 Webpage 서비스(Render)로 전송하고, 알림은 텔레그램으로 보낸다.
이 프로그램은 웹 서버를 열지 않는다.
"""

import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os
import re
import time
import queue
import random
import logging
import threading
import argparse
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

import urllib3
import urllib.request
from browser_background import default_profile_directory, supports_background
from web_publisher import WebPublisher
from sungnam_client import (
    SungnamClient, BrowserLaunchError, AccessBlocked, LoginFailed, ManualLoginRequired, SessionExpired,
    HUMAN_DELAY_RANGE, parse_timetable as sn_parse_timetable,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The system proxy doesn't exempt 127.0.0.1, which breaks nodriver's DevTools connection.
# Once *_proxy env vars exist, urllib ignores the registry, so copy the registry proxies first.
for _scheme, _url in urllib.request.getproxies().items():
    if _scheme != "no":
        os.environ.setdefault(f"{_scheme}_proxy", _url)
os.environ["no_proxy"] = ",".join(
    [p for p in os.environ.get("no_proxy", "").split(",") if p] + ["127.0.0.1", "localhost"]
)

# ─────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
SUNGNAM_DIR  = os.path.join(_HERE, "Sungnam")
LOG_DIR      = os.path.join(_HERE, "log_all")
KST          = timezone(timedelta(hours=9))


SN_BASE_URL  = "https://res.isdc.co.kr"
YN_BASE_URL  = "https://publicsports.yongin.go.kr/publicsports"
YN_TIME_API  = f"{YN_BASE_URL}/sports/selectRegistTimeByChosenDateFcltyRceptResveApply.do"
YN_PAGE_SIZE = 8
YN_WORKERS   = 8
# 성남·용인 모두 한 번 조회를 마친 뒤 이 범위에서 무작위로 쉬고 다시 조회한다.
POLL_MIN_SECONDS = 400
POLL_MAX_SECONDS = 800

def next_poll_delay():
    return random.randint(POLL_MIN_SECONDS, POLL_MAX_SECONDS)

NODRIVER_PROFILE = default_profile_directory(_HERE)
SN_MANUAL_LOGIN = False
SN_BACKGROUND = supports_background()
SN_BROWSER_EXECUTABLE = os.environ.get("CHROME_BIN")
# ─────────────────────────────────────────────────────────
# 공유 상태 (수집 스레드가 쓰고, 웹 전송 스레드가 읽는다)
# ─────────────────────────────────────────────────────────
_lock = threading.Lock()
_sn_stop_event = threading.Event()
_sn_client = None

_sn_available   = []
_sn_courts      = []
_sn_last_update = ""
_sn_status      = "starting"
_sn_error       = ""
_sn_collected   = {}
_sn_targets     = {}
_sn_cycle_done  = set()

_yn_available   = []
_yn_courts      = []
_yn_last_update = ""
_yn_period      = ""
_yn_status      = "starting"
_yn_error       = ""

# ─────────────────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────────────────
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    ts       = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"all_{ts}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ─────────────────────────────────────────────────────────
# 공통 계정 로더 (root/auth.txt, [sungnam]/[yongin] 섹션)
# ─────────────────────────────────────────────────────────
ROOT_AUTH_FILE = os.path.join(_HERE, "auth.txt")

def _load_auth_section(section):
    """root/auth.txt 에서 [section] 아래의 id,password 목록 반환"""
    path = ROOT_AUTH_FILE
    if not os.path.exists(path):
        return []
    accounts = []
    in_section = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower() == f"[{section}]":
                in_section = True
                continue
            if line.startswith("["):
                if in_section:
                    break  # 다음 섹션 만나면 종료
                continue
            if in_section and "," in line:
                u, p = line.split(",", 1)
                accounts.append({"username": u.strip(), "password": p.strip()})
    return accounts


def _load_accounts_from_env(prefix):
    """연디 id{prefix}1~3 / pwd{prefix}1~3 환경변수에서 계정 목록 반환.
    하나라도 존재하면 해당 목록만 반환 (비어있으면 None 반환)."""
    accounts = []
    for i in range(1, 4):
        uid = os.environ.get(f"id{prefix}{i}", "").strip()
        pwd = os.environ.get(f"pwd{prefix}{i}", "").strip()
        if uid and pwd:
            accounts.append({"username": uid, "password": pwd})
    return accounts if accounts else None


# ═══════════════════════════════════════════════════════════
# SUNGNAM 모니터링
# ═══════════════════════════════════════════════════════════


def sn_load_accounts():
    env = _load_accounts_from_env("Sungnam")
    if env:
        logging.info(f"[SN] 계정 환경변수에서 로드: {[a['username'] for a in env]}")
        return env
    return _load_auth_section("sungnam")


def sn_load_monitoring_table():
    mon_file   = os.path.join(SUNGNAM_DIR, "MonitoringTable.txt")
    facilities = []
    if not os.path.exists(mon_file):
        return facilities
    current_fac = None
    section     = "weekday"
    with open(mon_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("FAC"):
                m = re.match(r"(FAC\d+)\(([^)]+)\)", line)
                if m:
                    current_fac = m.group(1)
                    section     = "weekday"
                    facilities.append({"id": m.group(1), "name": m.group(2),
                                       "weekday_times": [], "weekend_times": []})
            elif line == "주중":
                section = "weekday"
            elif line == "주말":
                section = "weekend"
            elif current_fac and line.lower() == "all":
                key = "weekday_times" if section == "weekday" else "weekend_times"
                facilities[-1][key] = ["ALL"]
            elif current_fac and ":" in line and "~" in line and not line.startswith("#"):
                key = "weekday_times" if section == "weekday" else "weekend_times"
                facilities[-1][key].append(line)
    return facilities

def sn_make_session():
    return SungnamClient(NODRIVER_PROFILE, manual_login=SN_MANUAL_LOGIN, background=SN_BACKGROUND,
                         browser_executable=SN_BROWSER_EXECUTABLE)


def sn_login(session, username, password):
    try:
        return session.login(username, password)
    except LoginFailed as exc:
        logging.warning("[SN] %s", exc)
        return False


def sn_get_timetable(session, facility_id, date_str):
    return session.get_timetable(facility_id, date_str)


def _sn_ensure_login(session, accounts):
    if getattr(session, "manual_login", False) is True:
        return session.login("", "")
    return any(sn_login(session, acc["username"], acc["password"]) for acc in accounts)


def sn_time_match(slot_time, target_time):
    """슬롯 시간이 target 조건에 해당하는지 확인.
    target 형식:
      - 'HH:MM ~ HH:MM'  → 정확한 시작/종료 일치
      - '~HH:MM'          → 슬롯 종료시간 ≤ HH:MM
      - 'HH:MM~'          → 슬롯 시작시간 ≥ HH:MM
    """
    try:
        parts = slot_time.replace("～", "~").split("~")
        s1 = parts[0].strip()
        e1 = parts[1].strip() if len(parts) > 1 else ""
        def to_min(t): h, m = map(int, t.split(":")); return h*60+m
        target = target_time.strip().replace("～", "~")
        if target.startswith("~"):
            # 종료시간 이하
            return to_min(e1) <= to_min(target[1:].strip())
        elif target.endswith("~"):
            # 시작시간 이상
            return to_min(s1) >= to_min(target[:-1].strip())
        else:
            # 정확한 범위 일치
            s2, e2 = [t.strip() for t in target.split("~")]
            return s1 == s2 and e1 == e2
    except Exception:
        return False


_DOW_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def sn_run_once(accounts, facilities, session=None, on_result=None):
    """성남 모니터링 1회. → (available_list, all_courts_list)"""
    if session is not None:
        return _sn_scan(session, accounts, facilities, on_result=on_result)
    session = sn_make_session()
    try:
        return _sn_scan(session, accounts, facilities, on_result=on_result)
    finally:
        session.close()


def _sn_scan(session, accounts, facilities, on_result=None):
    # 로그인
    logged_in = session.authenticated
    if not logged_in:
        logged_in = _sn_ensure_login(session, accounts)
    if not logged_in:
        raise LoginFailed("성남 모든 계정 로그인 실패")

    all_available = []
    all_courts    = []
    today         = datetime.now(KST)
    relogin_used  = False

    for fac in facilities:
        for i in range(4):
            date       = today + timedelta(days=i)
            date_str   = date.strftime("%Y-%m-%d")
            is_weekend = date.weekday() >= 5
            dow        = _DOW_KO[date.weekday()]
            time_slots = fac["weekend_times"] if is_weekend else fac["weekday_times"]
            if not time_slots:
                continue

            try:
                html = sn_get_timetable(session, fac["id"], date_str)
            except SessionExpired:
                if relogin_used:
                    raise SessionExpired("성남 재로그인 후에도 세션이 만료되어 이번 조회를 중단합니다.")
                relogin_used = True
                logging.warning("[SN] 세션 만료 확인 — 이번 조회에서 1회만 재로그인합니다.")
                if not _sn_ensure_login(session, accounts):
                    raise LoginFailed("성남 세션 복구 실패")
                html = sn_get_timetable(session, fac["id"], date_str)

            avail_slots, all_slot_list = sn_parse_timetable(html)
            courts_start, available_start = len(all_courts), len(all_available)

            for slot in all_slot_list:
                all_courts.append({
                    "date":             date_str,
                    "day_of_week":      dow,
                    "facility_name":    fac["name"],
                    "fac_id":           fac["id"],
                    "court":            slot["court"],
                    "time":             slot["time"],
                    "is_available":     slot["is_available"],
                    "reservation_name": slot["reservation_name"],
                })

            for slot in avail_slots:
                if "ALL" in time_slots or any(sn_time_match(slot["time"], t) for t in time_slots):
                    all_available.append({
                        "date":          date_str,
                        "day_of_week":   dow,
                        "facility_name": fac["name"],
                        "fac_id":        fac["id"],
                        "court":         slot["court"],
                        "time":          slot["time"],
                    })

            if on_result is not None:
                on_result(fac["id"], date_str, all_available[available_start:], all_courts[courts_start:])

            logging.info(f"[SN] {fac['name']} {date_str}: 예약가능 "
                         f"{sum(1 for s in avail_slots if 'ALL' in time_slots or any(sn_time_match(s['time'], t) for t in time_slots))}개")
            time.sleep(random.uniform(*HUMAN_DELAY_RANGE))

    return all_available, all_courts


def _sn_refresh_snapshot():
    """호출자는 _lock을 보유한다. 실패한 구간은 마지막 성공 자료를 유지한다."""
    global _sn_available, _sn_courts, _sn_last_update
    _sn_available, _sn_courts = [], []
    for key in _sn_targets:
        snapshot = _sn_collected.get(key)
        if snapshot is None:
            continue
        metadata = {"last_checked": snapshot["last_update"], "is_previous": key not in _sn_cycle_done}
        _sn_available.extend(dict(row, **metadata) for row in snapshot["available"])
        _sn_courts.extend(dict(row, **metadata) for row in snapshot["courts"])
    _sn_last_update = max((value["last_update"] for value in _sn_collected.values()), default="")


def _sn_begin_cycle(facilities):
    global _sn_targets, _sn_cycle_done
    today = datetime.now(KST)
    targets = {}
    for fac in facilities:
        for offset in range(4):
            date = today + timedelta(days=offset)
            times = fac["weekend_times"] if date.weekday() >= 5 else fac["weekday_times"]
            if times:
                targets[fac["id"], date.strftime("%Y-%m-%d")] = fac["name"]
    with _lock:
        _sn_targets, _sn_cycle_done = targets, set()
        if _sn_collected:
            for key in list(_sn_collected):
                if key not in targets:
                    del _sn_collected[key]
            _sn_refresh_snapshot()


def _sn_publish_result(facility_id, date, available, courts):
    global _sn_error
    key = facility_id, date
    with _lock:
        if key not in _sn_targets:
            return
        _sn_collected[key] = {"available": list(available), "courts": list(courts),
                              "last_update": datetime.now(KST).isoformat()}
        _sn_cycle_done.add(key)
        _sn_error = ""
        _sn_refresh_snapshot()


def _sn_coverage():
    """호출자는 _lock을 보유한다. 조회 성공 0건과 미수집을 구분한다."""
    return [{"fac_id": key[0], "date": key[1], "facility_name": name,
             "status": "current" if key in _sn_cycle_done else "previous" if key in _sn_collected else "missing",
             "last_update": _sn_collected.get(key, {}).get("last_update", "")}
            for key, name in _sn_targets.items()]


# ─────────────────────────────────────────────────────────
# Telegram 알림
# ─────────────────────────────────────────────────────────
NOTIFY_TABLE     = os.path.join(_HERE, "NotifyTable.txt")        # 성남+용인 텔레그램 알림

_tg_bot_token = ""
_tg_chat_id   = ""

def load_telegram_config():
    global _tg_bot_token, _tg_chat_id
    # 1순위: 환경변수
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    env_chat  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_token and env_chat:
        _tg_bot_token = env_token
        _tg_chat_id   = env_chat
        logging.info(f"[TG] 텔레그램 설정 환경변수에서 로드 (token={_tg_bot_token[:15]}...)")
        return
    # 2순위: auth.txt [Telegram] 섹션
    if not os.path.exists(ROOT_AUTH_FILE):
        logging.warning("[TG] auth.txt 없음 + 환경변수 미설정 – 알림 비활성화")
        return
    in_section = False
    with open(ROOT_AUTH_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower() == "[telegram]":
                in_section = True
                continue
            if line.startswith("["):
                if in_section:
                    break
                continue
            if in_section and "=" in line:
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if key == "TELEGRAM_BOT_TOKEN" and val not in ("", "your_bot_token_here"):
                    _tg_bot_token = val
                elif key == "TELEGRAM_CHAT_ID" and val not in ("", "your_chat_id_here"):
                    _tg_chat_id = val
    if _tg_bot_token and _tg_chat_id:
        logging.info(f"[TG] 텔레그램 설정 로드 완료 (token={_tg_bot_token[:15]}...)")
    else:
        logging.warning("[TG] 토큰/채팅ID 미설정 – 알림 비활성화")


def _tg_escape(text):
    """MarkdownV2 이스케이프"""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text


def send_telegram(message_text):
    """MarkdownV2 형식으로 텔레그램 메시지 전송"""
    if not _tg_bot_token or not _tg_chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{_tg_bot_token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": _tg_chat_id,
            "text": message_text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }, timeout=10, verify=False)
        if resp.status_code == 200 and resp.json().get("ok"):
            logging.info("[TG] 알림 전송 성공")
        else:
            logging.warning(f"[TG] 전송 실패: {resp.text[:200]}")
    except Exception as e:
        logging.error(f"[TG] 전송 오류: {e}")


def _section_lines(filepath, section_name):
    """filepath 에서 [section_name] 블록 내의 줄만 반환 (빈 줄 포함)."""
    lines, in_sec = [], False
    if not os.path.exists(filepath):
        return lines
    with open(filepath, encoding="utf-8") as f:
        for raw in f:
            ln = raw.rstrip("\n")
            stripped = ln.strip()
            if stripped == f"[{section_name}]":
                in_sec = True; continue
            if stripped.startswith("[") and stripped.endswith("]"):
                if in_sec: break
                continue
            if in_sec:
                lines.append(stripped)
    return lines


def _split_telegram_flag(line):
    """'18:00~ (T)' → ('18:00~', True). (T) 가 붙은 규칙만 텔레그램 알림 대상."""
    m = re.search(r"\s*\(T\)\s*$", line, re.IGNORECASE)
    return (line[:m.start()].strip(), True) if m else (line.strip(), False)


def sn_load_notify_table():
    """NotifyTable.txt [sungnam] 섹션 파싱. *_times = 조회 대상 전체, *_notify = (T) 표시된 시간대"""
    facs = []
    current_fac = None
    section     = "weekday"
    for line in _section_lines(NOTIFY_TABLE, "sungnam"):
        if not line or line.startswith("//"):
            continue
        text, telegram = _split_telegram_flag(line)
        if line.startswith("FAC"):
            m = re.match(r"(FAC\d+)\(([^)]+)\)", line)
            if m:
                current_fac = m.group(1)
                section     = "weekday"
                facs.append({"id": m.group(1), "name": m.group(2),
                             "weekday_times": [], "weekend_times": [],
                             "weekday_notify": [], "weekend_notify": []})
        elif line == "주중":
            section = "weekday"
        elif line == "주말":
            section = "weekend"
        elif current_fac and text.lower() == "all":
            facs[-1][f"{section}_times"] = ["ALL"]
            if telegram:
                facs[-1][f"{section}_notify"] = ["ALL"]
        elif current_fac and ":" in text and "~" in text and not text.startswith("#"):
            facs[-1][f"{section}_times"].append(text)
            if telegram:
                facs[-1][f"{section}_notify"].append(text)
    return facs


def sn_passes_notify(slot, notify_facs):
    """성남 슬롯이 (T) 알림 조건에 해당하는지 확인"""
    fac_id = slot.get("fac_id", "")
    dow    = slot.get("day_of_week", "")
    t      = slot.get("time", "")
    weekend = dow in ("토요일", "일요일")
    for fac in notify_facs:
        if fac["id"] != fac_id:
            continue
        times = fac["weekend_notify"] if weekend else fac["weekday_notify"]
        if "ALL" in times:
            return True
        return any(sn_time_match(t, ft) for ft in times)
    return False


def yn_load_notify_table():
    """NotifyTable.txt [yongin] 섹션 파싱. weekday/weekend_all = 조회 규칙 전체, notify_* = (T) 규칙만"""
    rules   = {}
    cur_gu  = None
    section = None
    for line in _section_lines(NOTIFY_TABLE, "yongin"):
        if line.startswith("#"):
            continue
        if not line:
            cur_gu = None; section = None; continue
        if line.endswith("구"):
            cur_gu = line; section = None; continue
        if cur_gu is None:
            continue
        text, telegram = _split_telegram_flag(line)
        if line in ("주중", "주말"):
            section = "weekday" if line == "주중" else "weekend"
            rules.setdefault(cur_gu, {"weekday": [], "weekend_all": False,
                                      "notify_weekday": [], "notify_weekend_all": False})
        elif section == "weekday" and (text.startswith("~") or text.endswith("~")):
            rules[cur_gu]["weekday"].append(text)
            if telegram:
                rules[cur_gu]["notify_weekday"].append(text)
        elif section == "weekend" and text.lower() == "all":
            rules[cur_gu]["weekend_all"] = True
            if telegram:
                rules[cur_gu]["notify_weekend_all"] = True
    return rules


def _courts_key(courts):
    """예약 가능 코트 목록을 비교용 문자열로 변환"""
    return "|".join(
        f"{c.get('date','')}/{c.get('facility_name', c.get('court_name',''))}/{c.get('court', c.get('time',''))}/{c.get('time','')}"
        for c in sorted(courts, key=lambda x: (
            x.get('date',''), x.get('facility_name', x.get('court_name','')),
            x.get('court', ''), x.get('time','')))
    )


# ═══════════════════════════════════════════════════════════
# YONGIN 모니터링
# ═══════════════════════════════════════════════════════════

YN_AUTH_FILE = ROOT_AUTH_FILE


def yn_make_session():
    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def yn_group_login(session, user_id, password):
    try:
        resp = session.post(
            f"{YN_BASE_URL}/groupLogin.do",
            data={"id": user_id, "password": password},
            headers={"Referer": f"{YN_BASE_URL}/loginForm.do?groupYn=Y",
                     "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True, timeout=15,
        )
        msgs = re.findall(r'decodeURIComponent\("([^"]+)"\)', resp.text)
        if msgs:
            msg = unquote(msgs[0])
            if "성공" in msg:
                logging.info(f"[YN] ✅ 로그인 성공 (ID: {user_id})")
                return True
            return False
        if "loginForm" not in resp.url:
            return True
        return False
    except Exception as e:
        logging.error(f"[YN] 로그인 오류: {e}")
        return False


def yn_load_credentials():
    env = _load_accounts_from_env("Yongin")
    if env:
        logging.info(f"[YN] 계정 환경변수에서 로드: {[a['username'] for a in env]}")
        return [(a["username"], a["password"]) for a in env]
    return [(a["username"], a["password"]) for a in _load_auth_section("yongin")]


def yn_fetch_courts():
    sess = yn_make_session()
    try:
        return _yn_fetch_courts(sess)
    finally:
        sess.close()


def _yn_fetch_courts(sess):
    courts = []
    seen_ids = set()
    page_idx = 1

    while True:
        url = (f"{YN_BASE_URL}/sports/selectFcltyRceptResveListU.do"
               f"?key=4292&searchResveType=GNRLRESVE"
               f"&pageUnit={YN_PAGE_SIZE}&pageIndex={page_idx}")
        try:
            resp = sess.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Referer": f"{YN_BASE_URL}/sports/selectFcltyRceptResveListU.do?key=4292",
                },
                timeout=20,
            )
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"용인 코트 목록 {page_idx}쪽 요청 실패. 수집 중인 일부 목록은 게시하지 않습니다: {e}") from e

        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select("div.popup, div.layer, div.layer_wrap"):
            el.decompose()

        items = soup.select("li.reserve_box_item")
        if not items:
            title = soup.title.get_text(strip=True) if soup.title else ""
            snippet = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:500]
            raise RuntimeError(
                f"[YN] 코트 목록 HTML 파싱 실패 page={page_idx} "
                f"status={resp.status_code} url={resp.url} title={title!r} body={snippet!r}"
            )

        page_added = 0
        for item in items:
            link = item.select_one('a[href*="resveId"]')
            if not link:
                continue
            m = re.search(r"resveId=(\d+)", link.get("href", ""))
            if not m:
                continue
            resve_id = m.group(1)
            if resve_id in seen_ids:
                continue
            seen_ids.add(resve_id)

            title_div = item.select_one(".reserve_title")
            position_div = item.select_one(".reserve_position")
            location = position_div.get_text(strip=True) if position_div else ""
            if position_div:
                position_div.decompose()
            court_name = title_div.get_text(strip=True) if title_div else "알 수 없음"
            courts.append({"resve_id": resve_id, "name": court_name, "location": location})
            page_added += 1

        logging.info(f"[YN] 코트 목록 page={page_idx}: items={len(items)} added={page_added}")
        if page_added == 0:
            raise RuntimeError(f"용인 코트 목록 {page_idx}쪽에서 같은 목록이 반복되어 수집을 중단합니다.")
        if len(items) < YN_PAGE_SIZE:
            break
        page_idx += 1
        time.sleep(0.1)

    tennis_courts = [c for c in courts if "테니스" in c["name"]]
    if courts and not tennis_courts:
        logging.warning(f"[YN] 테니스 코트 필터 결과 0건. 샘플: {[c['name'] for c in courts[:5]]}")
    return tennis_courts


def yn_get_time_slots(session, resve_id, apply_url, date_yyyymmdd):
    try:
        r = session.post(
            YN_TIME_API,
            data={"dateVal": date_yyyymmdd, "resveId": resve_id},
            headers={"Referer": apply_url,
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data      = r.json()
        available = [{"time": s.get("timeContent", "")} for s in data.get("resveTmList", [])]
        all_slots = [{"time": s.get("useTm", ""), "status": s.get("rsvctmStts", ""),
                      "name": s.get("frstRegisterNmApply", "")}
                     for s in data.get("fcltRceptRsvctmTime", [])]
        if not all_slots and not available:
            return {"available": [], "all": [], "date_str": "", "day_of_week": "",
                    "outside_range": True}
        return {"date_str": data.get("formatedDate", ""),
                "day_of_week": data.get("formatedDay", ""),
                "available": available, "all": all_slots}
    except Exception as e:
        logging.warning(f"[YN] 시간대 오류 {resve_id} {date_yyyymmdd}: {e}")
        return None


def yn_scan_one_court(court, target_dates, sess_holder, creds, cred_idx):
    resve_id  = court["resve_id"]
    apply_url = (f"{YN_BASE_URL}/sports/selectFcltyRceptResveApplyListU.do"
                 f"?key=4292&searchResveId={resve_id}")
    try:
        sess_holder[0].get(apply_url, timeout=15)
    except Exception:
        pass

    available  = []
    court_data = []

    for target_date in target_dates:
        date_yyyymmdd = target_date.strftime("%Y%m%d")
        result = yn_get_time_slots(sess_holder[0], resve_id, apply_url, date_yyyymmdd)
        if result is None:
            for i in range(len(creds)):
                uid, pw  = creds[(cred_idx + i) % len(creds)]
                new_sess = yn_make_session()
                if yn_group_login(new_sess, uid, pw):
                    sess_holder[0] = new_sess
                    result = yn_get_time_slots(sess_holder[0], resve_id, apply_url, date_yyyymmdd)
                    break
        if result is None or result.get("outside_range"):
            continue

        date_str    = result["date_str"]
        day_of_week = result["day_of_week"]
        merged      = {}

        for slot in result["available"]:
            t          = slot["time"]
            merged[t]  = {"resve_id": resve_id, "court_name": court["name"],
                          "location": court["location"], "date": date_str,
                          "day_of_week": day_of_week, "time": t,
                          "status": "", "is_available": True}
        for slot in result["all"]:
            t = slot["time"]
            if t not in merged:
                merged[t] = {"resve_id": resve_id, "court_name": court["name"],
                             "location": court["location"], "date": date_str,
                             "day_of_week": day_of_week, "time": t,
                             "status": slot["status"], "is_available": False}

        for entry in merged.values():
            court_data.append(entry)
            if entry["is_available"]:
                available.append(entry)

    return available, court_data


def yn_dates_until_end_of_month():
    import calendar
    today    = datetime.now(KST)
    last_day = calendar.monthrange(today.year, today.month)[1]
    days     = (today.replace(day=last_day) - today).days + 1
    return [today + timedelta(days=i) for i in range(days)]


def yn_passes_filter(entry, table, notify=False):
    """notify=False: 조회 규칙 전체, notify=True: (T) 규칙만"""
    if not table:
        return True
    loc        = entry.get("location", "")
    matched_gu = next((g for g in table if g in loc), None)
    if matched_gu is None:
        return False
    spec    = table[matched_gu]
    dow     = entry.get("day_of_week", "")
    weekend = dow in ("토요일", "일요일")
    prefix  = "notify_" if notify else ""
    if weekend:
        return spec.get(prefix + "weekend_all", False)
    try:
        parts = entry.get("time", "").split("~")
        sm = sum(int(x) * m for x, m in zip(parts[0].strip().split(":"), [60, 1]))
        em = sum(int(x) * m for x, m in zip(parts[1].strip().split(":"), [60, 1]))
    except Exception:
        return False
    for rule in spec.get(prefix + "weekday", []):
        if rule.startswith("~"):
            lh, lm = map(int, rule[1:].split(":"))
            if em <= lh * 60 + lm:
                return True
        elif rule.endswith("~"):
            lh, lm = map(int, rule[:-1].split(":"))
            if sm >= lh * 60 + lm:
                return True
    return False


def yn_run_once():
    """용인 모니터링 1회. → (available, all_courts, period_str)"""
    creds = yn_load_credentials()
    if not creds:
        logging.error("[YN] auth.txt 에 [yongin] 계정 없음")
        return [], [], ""

    courts = yn_fetch_courts()
    if not courts:
        logging.error("[YN] 코트 목록 없음")
        return [], [], ""

    mon_table = yn_load_notify_table()
    if mon_table:
        before = len(courts)
        courts = [c for c in courts if any(g in c["location"] for g in mon_table)]
        logging.info(f"[YN] 코트 필터: {before}→{len(courts)}")

    target_dates = yn_dates_until_end_of_month()
    period_str   = (f"{target_dates[0].strftime('%Y-%m-%d')} ~ "
                    f"{target_dates[-1].strftime('%Y-%m-%d')} ({len(target_dates)}일)")

    n_workers = min(YN_WORKERS, len(courts))
    sess_pool = queue.Queue()
    for i in range(n_workers):
        s = yn_make_session()
        uid, pw = creds[i % len(creds)]
        if not yn_group_login(s, uid, pw):
            for uid2, pw2 in creds:
                if yn_group_login(s, uid2, pw2):
                    break
        sess_pool.put([s])

    all_available  = []
    all_court_data = []
    completed      = 0

    def _worker(court, cred_idx):
        holder = sess_pool.get()
        try:
            return yn_scan_one_court(court, target_dates, holder, creds, cred_idx)
        finally:
            sess_pool.put(holder)

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker, c, i % len(creds)): c
                   for i, c in enumerate(courts)}
        for fut in as_completed(futures):
            court = futures[fut]
            try:
                a, d = fut.result()
                all_available.extend(a)
                all_court_data.extend(d)
            except Exception as exc:
                logging.error(f"[YN] 워커 오류 [{court['name']}]: {exc}")
            completed += 1
            logging.info(f"[YN] ✅ {completed}/{len(courts)}: {court['name']}")

    if mon_table:
        all_available  = [e for e in all_available  if yn_passes_filter(e, mon_table)]
        all_court_data = [e for e in all_court_data if yn_passes_filter(e, mon_table)]
        logging.info(f"[YN] 시간 필터 적용: 예약가능 {len(all_available)}개")

    return all_available, all_court_data, period_str


# ─────────────────────────────────────────────────────────
# 백그라운드 모니터링 루프
# ─────────────────────────────────────────────────────────
def _notify_if_changed(label, new_courts, prev_key_holder, build_msg_fn):
    """new_courts 가 이전과 달라지면 텔레그램 전송. prev_key_holder = [last_key]"""
    if not new_courts:
        # 모두 사라졌다가 다시 열리는 경우(취소)도 새 알림을 보내기 위해 이전 목록을 비운다.
        prev_key_holder[0] = ""
        return
    key = _courts_key(new_courts)
    if key != prev_key_holder[0]:
        prev_key_holder[0] = key
        msg = build_msg_fn(new_courts)
        logging.info(f"[TG] {label} 변경 감지 → 알림 전송")
        send_telegram(msg)


def _sn_build_msg(courts):
    lines = [_tg_escape("🎾 [성남] 예약 가능한 관심 코트 발견!"), ""]
    by_date = {}
    for c in courts:
        by_date.setdefault(c["date"], {}).setdefault(c["facility_name"], []).append(c)
    dow_map = {c["date"]: c["day_of_week"] for c in courts}
    for dt in sorted(by_date):
        dow = {"월요일":"월","화요일":"화","수요일":"수","목요일":"목",
               "금요일":"금","토요일":"토","일요일":"일"}.get(dow_map.get(dt,""), "")
        lines.append(f"*{_tg_escape(dt)} \\({_tg_escape(dow)}\\)*")
        for fn, slots in sorted(by_date[dt].items()):
            lines.append(f"  🏟 {_tg_escape(fn)}")
            for s in sorted(slots, key=lambda x: x["time"]):
                lines.append(f"    ✓ {_tg_escape(s['court'])}  {_tg_escape(s['time'])}")
    lines += ["", _tg_escape("https://res.isdc.co.kr/")]
    return "\n".join(lines)


def _yn_build_msg(courts):
    lines = [_tg_escape("🎾 [용인] 예약 가능한 관심 코트 발견!"), ""]
    by_date = {}
    for c in courts:
        by_date.setdefault(c["date"], {}).setdefault(c["location"], {}).setdefault(c["court_name"], []).append(c)
    dow_map = {c["date"]: c["day_of_week"] for c in courts}
    for dt in sorted(by_date):
        dow = dow_map.get(dt, "")
        lines.append(f"*{_tg_escape(dt)} \\({_tg_escape(dow)}\\)*")
        for loc, names in sorted(by_date[dt].items()):
            for name, slots in sorted(names.items()):
                short = name.replace("[유료]","").replace("[무료]","").split("_")[0].strip()
                lines.append(f"  🏟 {_tg_escape(short)}  _{_tg_escape(loc)}_")
                for s in sorted(slots, key=lambda x: x["time"]):
                    lines.append(f"    ✓ {_tg_escape(s['time'])}")
    lines += ["", _tg_escape("https://publicsports.yongin.go.kr/")]
    return "\n".join(lines)


def sungnam_loop():
    global _sn_available, _sn_courts, _sn_last_update, _sn_status, _sn_error, _sn_client
    accounts     = sn_load_accounts()
    facilities   = sn_load_monitoring_table()
    notify_facs  = sn_load_notify_table()
    if not accounts and not SN_MANUAL_LOGIN:
        _sn_status, _sn_error = "error", "auth.txt에 성남 계정이 없습니다."
        logging.error("[SN] auth.txt 없음 – 성남 모니터링 비활성화")
        return
    if not facilities:
        if notify_facs:
            # MonitoringTable 없음 → NotifyTable 시설/시간대를 그대로 스캔
            facilities = notify_facs
            logging.warning(f"[SN] MonitoringTable 없음 – NotifyTable 시설 스캔: {[f['name'] for f in facilities]}")
        else:
            _sn_status, _sn_error = "error", "성남 조회 대상 시설 설정이 없습니다."
            logging.error("[SN] NotifyTable.txt 없음 – 성남 모니터링 비활성화")
            return
    if notify_facs:
        telegram = [f["name"] for f in notify_facs if f["weekday_notify"] or f["weekend_notify"]]
        logging.info(f"[SN] NotifyTable 로드: {[f['name'] for f in notify_facs]} / 텔레그램(T): {telegram}")
    else:
        logging.warning("[SN] NotifyTable.txt 없음 – 성남 텔레그램 알림 비활성화")
    session = sn_make_session()
    with _lock:
        _sn_client = session
    try:
        if not _sn_stop_event.is_set():
            _sungnam_monitor_loop(session, accounts, facilities, notify_facs)
    except asyncio.CancelledError:
        logging.info("[SN] 종료 요청을 받아 모니터용 Chrome을 정리합니다.")
    finally:
        try:
            session.close()
        finally:
            with _lock:
                _sn_client = None


def _sungnam_monitor_loop(session, accounts, facilities, notify_facs):
    global _sn_available, _sn_courts, _sn_last_update, _sn_status, _sn_error
    sn_prev_key = [""]
    while not _sn_stop_event.is_set():
        retry_delay = next_poll_delay()
        manual_login = getattr(session, "manual_login", False) is True
        try:
            _sn_begin_cycle(facilities)
            with _lock:
                if manual_login and not session.authenticated:
                    _sn_status, _sn_error = "waiting_login", "열린 Chrome에서 직접 로그인해 주세요."
                else:
                    _sn_status = "scanning"
            logging.info("[SN] ======= 성남 모니터링 시작 =======")
            avail, courts = sn_run_once(accounts, facilities, session=session, on_result=_sn_publish_result)
            with _lock:
                _sn_available   = avail
                _sn_courts      = courts
                _sn_last_update = datetime.now(KST).isoformat()
                _sn_status, _sn_error = "ok", ""
            logging.info(f"[SN] 완료: 예약가능 {len(avail)}개 / 전체 {len(courts)}개")
            # 알림 체크: courts 에서 notify 조건 맞는 가용 슬롯 추출
            if notify_facs:
                notify_slots = [c for c in courts
                                if c.get("is_available")
                                and sn_passes_notify(c, notify_facs)]
                logging.info(f"[SN] 알림 대상 슬롯: {len(notify_slots)}개")
                _notify_if_changed("[SN]", notify_slots, sn_prev_key, _sn_build_msg)
            logging.info("[SN] %s초 후 다시 조회합니다.", retry_delay)
        except Exception as e:
            with _lock:
                _sn_status = ("blocked" if isinstance(e, AccessBlocked) else
                              "waiting_login" if isinstance(e, ManualLoginRequired) else "error")
                _sn_error = str(e)
            if isinstance(e, BrowserLaunchError):
                logging.error("[SN] %s", e)
                logging.error("[SN] Chrome 시작 재시도를 중단합니다. 용인 모니터링은 계속됩니다.")
                return
            if isinstance(e, AccessBlocked) and not manual_login:
                # 차단은 시간이 지나면 풀리므로 중단하지 않고 다음 주기에 다시 조회한다.
                logging.warning("[SN] %s", e)
                logging.info("[SN] 이전 수집 결과를 유지합니다. %s초 후 다시 조회합니다.", retry_delay)
            elif isinstance(e, (AccessBlocked, LoginFailed)):
                if not manual_login:
                    logging.error("[SN] %s", e)
                    logging.error("[SN] 조회를 중단합니다. 이미 읽은 시간표는 웹 화면에 유지합니다. 사이트 안내를 확인하거나 --sn-manual-login으로 실행하세요.")
                    return
                retry_delay = 5
                logging.warning("[SN] %s", e)
                logging.info("[SN] 이전 수집 결과를 유지하고 Chrome에서 직접 로그인할 때까지 기다립니다.")
            else:
                logging.error(f"[SN] 루프 오류: {e}")
                logging.info("[SN] 이전 수집 결과를 유지합니다. %s초 후 재확인합니다.", retry_delay)
        session.wait(retry_delay)


def stop_sungnam(thread):
    """브라우저를 소유한 스레드에서 정리한 뒤 프로그램을 종료한다."""
    _sn_stop_event.set()
    with _lock:
        session = _sn_client
    if session is not None:
        session.request_stop()
    thread.join(timeout=10)
    if thread.is_alive():
        logging.warning("[SN] Chrome 종료 대기가 초과되었습니다.")


def yongin_loop():
    global _yn_available, _yn_courts, _yn_last_update, _yn_period, _yn_status, _yn_error
    notify_table = yn_load_notify_table()
    if notify_table:
        telegram = [gu for gu, r in notify_table.items() if r["notify_weekday"] or r["notify_weekend_all"]]
        logging.info(f"[YN] NotifyTable 로드: {list(notify_table.keys())} / 텔레그램(T): {telegram}")
    else:
        logging.warning("[YN] NotifyTable.txt 없음 – 용인 텔레그램 알림 비활성화")
    yn_prev_key = [""]
    while True:
        try:
            with _lock:
                _yn_status = "scanning"
            logging.info("[YN] ======= 용인 모니터링 시작 =======")
            avail, courts, period = yn_run_once()
            with _lock:
                _yn_available   = avail
                _yn_courts      = courts
                _yn_last_update = datetime.now(KST).isoformat()
                _yn_period      = period
                _yn_status, _yn_error = "ok", ""
            logging.info(f"[YN] 완료: 예약가능 {len(avail)}개 / 전체 {len(courts)}개")
            # 알림 체크: 조회된 avail 중 (T) 규칙에 맞는 슬롯만
            if notify_table:
                notify_slots = [e for e in avail if yn_passes_filter(e, notify_table, notify=True)]
                logging.info(f"[YN] 알림 대상 슬롯: {len(notify_slots)}개")
                _notify_if_changed("[YN]", notify_slots, yn_prev_key, _yn_build_msg)
        except Exception as e:
            with _lock:
                _yn_status, _yn_error = "error", str(e)
            logging.error(f"[YN] 루프 오류: {e}")
        delay = next_poll_delay()
        logging.info("[YN] %s초 후 다시 조회합니다.", delay)
        time.sleep(delay)


# ─────────────────────────────────────────────────────────
# 웹페이지 전송용 스냅샷
# ─────────────────────────────────────────────────────────
def build_sungnam_payload():
    with _lock:
        return {
            "available":   _sn_available,
            "all_courts":  _sn_courts,
            "last_update": _sn_last_update,
            "status":      _sn_status,
            "error":       _sn_error,
            "progress":    {"completed": len(_sn_cycle_done), "total": len(_sn_targets)},
            "coverage":    _sn_coverage(),
        }


def build_yongin_payload():
    with _lock:
        return {
            "available":   _yn_available,
            "all_courts":  _yn_courts,
            "last_update": _yn_last_update,
            "period":      _yn_period,
            "status":      _yn_status,
            "error":       _yn_error,
        }


def build_snapshot():
    return {"sungnam": build_sungnam_payload(), "yongin": build_yongin_payload()}


# ─────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────
def create_argument_parser():
    parser = argparse.ArgumentParser(description="성남+용인 테니스 코트 모니터링 (Webpage로 결과 전송)")
    parser.add_argument("--webpage-url", default=os.environ.get("WEBPAGE_URL"),
                        help="결과를 보낼 Webpage 주소 (기본: WEBPAGE_URL 환경변수). 인증 토큰은 WEBPAGE_INGEST_TOKEN 환경변수")
    login_mode = parser.add_mutually_exclusive_group()
    login_mode.add_argument("--sn-manual-login", dest="sn_manual_login", action="store_true",
                            help="성남 Chrome에서 직접 로그인하고 세션 재사용")
    login_mode.add_argument("--sn-auto-login", dest="sn_manual_login", action="store_false",
                            help="성남 저장 계정으로 자동 로그인 (기본값)")
    parser.set_defaults(sn_manual_login=False)
    window_mode = parser.add_mutually_exclusive_group()
    window_mode.add_argument("--sn-show-browser", dest="sn_background", action="store_false",
                             help="모니터용 Chrome과 시간표 창을 화면에 표시")
    window_mode.add_argument("--sn-background", dest="sn_background", action="store_true",
                             help="Windows 전용 작업 공간 / Linux Xvfb에서 Chrome 실행 (두 OS의 기본값)")
    parser.set_defaults(sn_background=supports_background())
    parser.add_argument("--sn-browser", default=os.environ.get("CHROME_BIN"),
                        help="Chrome/Chromium 실행 경로 (기본: CHROME_BIN 또는 자동 탐색)")
    parser.add_argument("--sn-profile", default=NODRIVER_PROFILE,
                        help="성남 전용 Chrome 프로필 폴더 (기본: 운영체제별 별도 폴더)")
    parser.add_argument("--no-sungnam", action="store_true",
                        help="성남 모니터링(Chrome)을 실행하지 않음 (환경변수 SUNGNAM_ENABLED=false와 동일)")
    return parser


def handle_termination(signum, frame):
    # Linux 서비스의 SIGTERM도 아래 finally를 거쳐 전용 브라우저를 정리한다.
    logging.info("종료 신호를 받았습니다 (signal=%s).", signum)
    raise SystemExit(0)


if __name__ == "__main__":
    args = create_argument_parser().parse_args()
    SN_MANUAL_LOGIN = args.sn_manual_login
    SN_BACKGROUND = args.sn_background
    SN_BROWSER_EXECUTABLE = args.sn_browser
    NODRIVER_PROFILE = os.path.abspath(os.path.expanduser(args.sn_profile))
    signal.signal(signal.SIGTERM, handle_termination)

    setup_logging()
    load_telegram_config()
    logging.info("=" * 60)
    logging.info("🎾 테니스 코트 모니터 시작")
    logging.info(f"   성남 설정: {SUNGNAM_DIR}")
    logging.info(f"   용인 설정: {NOTIFY_TABLE}")
    logging.info("   성남 로그인: %s", "직접 로그인 / 기존 세션 재사용" if SN_MANUAL_LOGIN else "저장된 계정으로 자동 로그인")
    logging.info("   성남 Chrome: %s", "백그라운드 실행" if SN_BACKGROUND and not SN_MANUAL_LOGIN else "화면 표시")
    logging.info("=" * 60)

    sungnam_enabled = (not args.no_sungnam
                       and os.environ.get("SUNGNAM_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"))
    if not sungnam_enabled:
        _sn_status, _sn_error = "error", "성남 모니터링이 비활성화되어 있습니다 (--no-sungnam / SUNGNAM_ENABLED=false)."
        logging.info("   성남 모니터링: 비활성화 (Chrome을 실행하지 않음)")

    # 백그라운드 모니터링 스레드 시작
    t_sn = threading.Thread(target=sungnam_loop, daemon=True, name="sungnam")
    t_yn = threading.Thread(target=yongin_loop,  daemon=True, name="yongin")
    publisher = None
    webpage_token = os.environ.get("WEBPAGE_INGEST_TOKEN", "").strip()
    if args.webpage_url and webpage_token:
        try:
            publisher = WebPublisher(args.webpage_url, webpage_token, build_snapshot)
            logging.info("   Webpage 전송: %s", publisher.endpoint)
        except ValueError as exc:
            logging.error("[WEB] %s – Webpage 전송을 사용하지 않습니다.", exc)
    else:
        logging.warning("[WEB] WEBPAGE_URL / WEBPAGE_INGEST_TOKEN이 없어 Webpage로 결과를 보내지 않습니다 (텔레그램 알림만 동작).")
    try:
        if sungnam_enabled:
            t_sn.start()
        t_yn.start()
        if publisher is not None:
            publisher.start()
        # 서비스 종료(SIGTERM/Ctrl+C)는 SystemExit/KeyboardInterrupt로 이 루프를 벗어난다.
        while True:
            time.sleep(1)
    finally:
        if publisher is not None:
            publisher.stop()
        if sungnam_enabled:
            stop_sungnam(t_sn)
