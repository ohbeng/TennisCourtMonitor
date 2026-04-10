#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
성남 + 용인 테니스 코트 예약 현황 통합 모니터링
  python tennis_court_monitor_all.py [--port 8000]
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
import logging
import threading
import argparse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
SUNGNAM_DIR  = os.path.join(_HERE, "Sungnam")
YONGIN_DIR   = os.path.join(_HERE, "Yongin")
LOG_DIR      = os.path.join(_HERE, "log_all")
KST          = timezone(timedelta(hours=9))

SN_BASE_URL  = "https://res.isdc.co.kr"
YN_BASE_URL  = "https://publicsports.yongin.go.kr/publicsports"
YN_TIME_API  = f"{YN_BASE_URL}/sports/selectRegistTimeByChosenDateFcltyRceptResveApply.do"
YN_PAGE_SIZE = 8
YN_WORKERS   = 8

# ─────────────────────────────────────────────────────────
# Flask 앱 & 공유 상태
# ─────────────────────────────────────────────────────────
app   = Flask(__name__)
_lock = threading.Lock()

_sn_available   = []
_sn_courts      = []
_sn_last_update = ""

_yn_available   = []
_yn_courts      = []
_yn_last_update = ""
_yn_period      = ""

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
    s        = requests.Session()
    s.verify = False
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    return s


def sn_login(session, username, password):
    try:
        resp = session.post(
            f"{SN_BASE_URL}/rest_loginCheck.do",
            data={"web_id": username, "web_pw": password},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{SN_BASE_URL}/login.do"},
            verify=False, timeout=15,
        )
        return resp.status_code == 200 and resp.text.strip() == "success"
    except Exception as e:
        logging.error(f"[SN] 로그인 오류: {e}")
        return False


def sn_get_timetable(session, facility_id, date_str):
    try:
        parts          = date_str.split("-")
        formatted_date = f"{parts[0]}-{int(parts[1])}-{int(parts[2])}"
        resp = session.post(
            f"{SN_BASE_URL}/otherTimetable.do",
            data={"facId": facility_id, "resdate": formatted_date},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{SN_BASE_URL}/reservationInfo.do"},
            verify=False, timeout=15,
        )
        if resp.status_code == 200 and "login.do" not in resp.url and "로그인" not in resp.text:
            return resp.text
        return None
    except Exception as e:
        logging.error(f"[SN] 타임테이블 오류 {facility_id} {date_str}: {e}")
        return None


def sn_parse_timetable(html):
    if not html:
        return [], []
    available_slots, all_slots = [], []
    court_sections = re.findall(
        r"<label class=\'tit required lb-timetable\'>.*?(\d+)번\s*코트.*?</label>"
        r".*?<div class=\'tableBox mgb30\'.*?<tbody>(.*?)</tbody>",
        html, re.DOTALL,
    )
    for court_num, content in court_sections:
        if "이용가능한 시간이 없습니다" in content:
            continue
        all_times = re.findall(
            r"<tr>\s*<td class=\'td-title\'>\s*(.*?)\s*</td>\s*<td class=\'td-title\'>(\d+)</td>"
            r"\s*<td class=\'td-title\'>(\d{1,2}:\d{2})\s*[~～]\s*(\d{1,2}:\d{2})</td>"
            r"\s*<td class=\'td-title\'>\s*(.*?)\s*</td>\s*</tr>",
            content,
        )
        for btn_html, _, start_t, end_t, rsvname in all_times:
            if len(start_t.split(":")[0]) == 1:
                start_t = "0" + start_t
            if len(end_t.split(":")[0]) == 1:
                end_t = "0" + end_t
            is_avail = "예약가능" in btn_html
            slot = {"court": f"{court_num}번 코트",
                    "time": f"{start_t} ~ {end_t}",
                    "is_available": is_avail,
                    "reservation_name": rsvname.strip()}
            all_slots.append(slot)
            if is_avail:
                available_slots.append(slot)
    return available_slots, all_slots


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


def sn_run_once(accounts, facilities):
    """성남 모니터링 1회. → (available_list, all_courts_list)"""
    # 로그인
    session   = sn_make_session()
    logged_in = False
    for acc in accounts:
        if sn_login(session, acc["username"], acc["password"]):
            logging.info(f"[SN] ✅ 로그인: {acc['username']}")
            logged_in = True
            break
    if not logged_in:
        logging.error("[SN] ❌ 모든 계정 로그인 실패")
        return [], []

    all_available = []
    all_courts    = []
    today         = datetime.now(KST)

    for i in range(4):
        date       = today + timedelta(days=i)
        date_str   = date.strftime("%Y-%m-%d")
        is_weekend = date.weekday() >= 5
        dow        = _DOW_KO[date.weekday()]

        for fac in facilities:
            time_slots = fac["weekend_times"] if is_weekend else fac["weekday_times"]
            if not time_slots:
                continue

            html = sn_get_timetable(session, fac["id"], date_str)
            if html is None:
                # 세션 만료 → 재로그인
                session = sn_make_session()
                for acc in accounts:
                    if sn_login(session, acc["username"], acc["password"]):
                        break
                html = sn_get_timetable(session, fac["id"], date_str)

            if not html:
                logging.warning(f"[SN] 타임테이블 없음: {fac['name']} {date_str}")
                continue

            avail_slots, all_slot_list = sn_parse_timetable(html)

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
                        "court":         slot["court"],
                        "time":          slot["time"],
                    })

            logging.info(f"[SN] {fac['name']} {date_str}: 예약가능 "
                         f"{sum(1 for s in avail_slots if 'ALL' in time_slots or any(sn_time_match(s['time'], t) for t in time_slots))}개")
            time.sleep(0.2)

    return all_available, all_courts


# ─────────────────────────────────────────────────────────
# Telegram 알림
# ─────────────────────────────────────────────────────────
MONITORING_TABLE = os.path.join(_HERE, "MonitoringTable.txt")   # 용인 스캔 필터
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


def sn_load_notify_table():
    """NotifyTable.txt [sungnam] 섹션 파싱"""
    facs = []
    current_fac = None
    section     = "weekday"
    for line in _section_lines(NOTIFY_TABLE, "sungnam"):
        if not line or line.startswith("//"):
            continue
        if line.startswith("FAC"):
            m = re.match(r"(FAC\d+)\(([^)]+)\)", line)
            if m:
                current_fac = m.group(1)
                section     = "weekday"
                facs.append({"id": m.group(1), "name": m.group(2),
                             "weekday_times": [], "weekend_times": []})
        elif line == "주중":
            section = "weekday"
        elif line == "주말":
            section = "weekend"
        elif current_fac and line.lower() == "all":
            key = "weekday_times" if section == "weekday" else "weekend_times"
            facs[-1][key] = ["ALL"]
        elif current_fac and ":" in line and "~" in line and not line.startswith("#"):
            key = "weekday_times" if section == "weekday" else "weekend_times"
            facs[-1][key].append(line)
    return facs


def sn_passes_notify(slot, notify_facs):
    """성남 슬롯이 notify 조건에 해당하는지 확인"""
    fac_id = slot.get("fac_id", "")
    dow    = slot.get("day_of_week", "")
    t      = slot.get("time", "")
    weekend = dow in ("토요일", "일요일")
    for fac in notify_facs:
        if fac["id"] != fac_id:
            continue
        times = fac["weekend_times"] if weekend else fac["weekday_times"]
        if "ALL" in times:
            return True
        return any(sn_time_match(t, ft) for ft in times)
    return False


def yn_load_notify_table():
    """NotifyTable.txt [yongin] 섹션 파싱"""
    rules   = {}
    cur_gu  = None
    section = None
    for line in _section_lines(NOTIFY_TABLE, "yongin"):
        if not line:
            cur_gu = None; section = None; continue
        if line.endswith("구"):
            cur_gu = line; section = None; continue
        if cur_gu is None:
            continue
        if line == "주중":
            section = "weekday"
            if cur_gu not in rules:
                rules[cur_gu] = {"weekday": [], "weekend_all": False}
        elif line == "주말":
            section = "weekend"
            if cur_gu not in rules:
                rules[cur_gu] = {"weekday": [], "weekend_all": False}
        elif section == "weekday" and (line.startswith("~") or line.endswith("~")):
            rules[cur_gu]["weekday"].append(line)
        elif section == "weekend" and line == "All":
            rules[cur_gu]["weekend_all"] = True
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
YN_MON_TABLE = MONITORING_TABLE


def yn_make_session():
    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
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
    sess    = yn_make_session()
    courts  = []
    page_idx = 1
    while True:
        url = (f"{YN_BASE_URL}/sports/selectFcltyRceptResveListU.do"
               f"?key=4292&searchResveType=GNRLRESVE"
               f"&pageUnit={YN_PAGE_SIZE}&pageIndex={page_idx}")
        try:
            resp = sess.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            logging.warning(f"[YN] 코트 목록 요청 실패: {e}")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select("div.popup, div.layer, div.layer_wrap"):
            el.decompose()
        items = soup.select("li.reserve_box_item")
        if not items:
            break
        for item in items:
            link = item.select_one('a[href*="resveId"]')
            if not link:
                continue
            m = re.search(r"resveId=(\d+)", link.get("href", ""))
            if not m:
                continue
            resve_id     = m.group(1)
            title_div    = item.select_one(".reserve_title")
            position_div = item.select_one(".reserve_position")
            location     = position_div.get_text(strip=True) if position_div else ""
            if position_div:
                position_div.decompose()
            court_name = title_div.get_text(strip=True) if title_div else "알 수 없음"
            courts.append({"resve_id": resve_id, "name": court_name, "location": location})
        if len(items) < YN_PAGE_SIZE:
            break
        page_idx += 1
        time.sleep(0.3)
    return [c for c in courts if "테니스" in c["name"]]


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


def yn_load_monitoring_table():
    """NotifyTable.txt [yongin] 섹션으로 모니터링 필터 적용"""
    rules   = {}
    cur_gu  = None
    section = None
    for line in _section_lines(NOTIFY_TABLE, "yongin"):
        if not line:
            cur_gu = None; section = None; continue
        if line.endswith("구"):
            cur_gu = line; section = None; continue
        if cur_gu is None:
            continue
        if line == "주중":
            section = "weekday"
            if cur_gu not in rules:
                rules[cur_gu] = {"weekday": [], "weekend_all": False}
        elif line == "주말":
            section = "weekend"
            if cur_gu not in rules:
                rules[cur_gu] = {"weekday": [], "weekend_all": False}
        elif section == "weekday" and (line.startswith("~") or line.endswith("~")):
            rules[cur_gu]["weekday"].append(line)
        elif section == "weekend" and line == "All":
            rules[cur_gu]["weekend_all"] = True
    return rules



def yn_passes_filter(entry, table):
    if not table:
        return True
    loc        = entry.get("location", "")
    matched_gu = next((g for g in table if g in loc), None)
    if matched_gu is None:
        return False
    spec    = table[matched_gu]
    dow     = entry.get("day_of_week", "")
    weekend = dow in ("토요일", "일요일")
    if weekend:
        return spec.get("weekend_all", False)
    try:
        parts = entry.get("time", "").split("~")
        sm = sum(int(x) * m for x, m in zip(parts[0].strip().split(":"), [60, 1]))
        em = sum(int(x) * m for x, m in zip(parts[1].strip().split(":"), [60, 1]))
    except Exception:
        return False
    for rule in spec.get("weekday", []):
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

    mon_table = yn_load_monitoring_table()
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
    global _sn_available, _sn_courts, _sn_last_update
    accounts     = sn_load_accounts()
    facilities   = sn_load_monitoring_table()
    notify_facs  = sn_load_notify_table()
    if not accounts:
        logging.error("[SN] auth.txt 없음 – 성남 모니터링 비활성화")
        return
    if not facilities:
        if notify_facs:
            # MonitoringTable 없음 → NotifyTable 시설/시간대를 그대로 스캔
            facilities = notify_facs
            logging.warning(f"[SN] MonitoringTable 없음 – NotifyTable 시설 스캔: {[f['name'] for f in facilities]}")
        else:
            logging.error("[SN] NotifyTable.txt 없음 – 성남 모니터링 비활성화")
            return
    if notify_facs:
        logging.info(f"[SN] NotifyTable 로드: {[f['name'] for f in notify_facs]}")
    else:
        logging.warning("[SN] NotifyTable.txt 없음 – 성남 텔레그램 알림 비활성화")
    sn_prev_key = [""]
    while True:
        try:
            logging.info("[SN] ======= 성남 모니터링 시작 =======")
            avail, courts = sn_run_once(accounts, facilities)
            with _lock:
                _sn_available   = avail
                _sn_courts      = courts
                _sn_last_update = datetime.now(KST).isoformat()
            logging.info(f"[SN] 완료: 예약가능 {len(avail)}개 / 전체 {len(courts)}개")
            # 알림 체크: courts 에서 notify 조건 맞는 가용 슬롯 추출
            if notify_facs:
                notify_slots = [c for c in courts
                                if c.get("is_available")
                                and sn_passes_notify(c, notify_facs)]
                logging.info(f"[SN] 알림 대상 슬롯: {len(notify_slots)}개")
                _notify_if_changed("[SN]", notify_slots, sn_prev_key, _sn_build_msg)
        except Exception as e:
            logging.error(f"[SN] 루프 오류: {e}")
        time.sleep(90)


def yongin_loop():
    global _yn_available, _yn_courts, _yn_last_update, _yn_period
    notify_table = yn_load_notify_table()
    if notify_table:
        logging.info(f"[YN] NotifyTable 로드: {list(notify_table.keys())}")
    else:
        logging.warning("[YN] NotifyTable.txt 없음 – 용인 텔레그램 알림 비활성화")
    yn_prev_key = [""]
    while True:
        try:
            logging.info("[YN] ======= 용인 모니터링 시작 =======")
            avail, courts, period = yn_run_once()
            with _lock:
                _yn_available   = avail
                _yn_courts      = courts
                _yn_last_update = datetime.now(KST).isoformat()
                _yn_period      = period
            logging.info(f"[YN] 완료: 예약가능 {len(avail)}개 / 전체 {len(courts)}개")
            # 알림 체크: avail 중 notify 조건 맞는 슬롯 (MonitoringTable 이미 필터됨)
            if notify_table:
                notify_slots = [e for e in avail if yn_passes_filter(e, notify_table)]
                logging.info(f"[YN] 알림 대상 슬롯: {len(notify_slots)}개")
                _notify_if_changed("[YN]", notify_slots, yn_prev_key, _yn_build_msg)
        except Exception as e:
            logging.error(f"[YN] 루프 오류: {e}")
        time.sleep(300)


# ─────────────────────────────────────────────────────────
# HTML 템플릿
# ─────────────────────────────────────────────────────────
_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>테니스 코트 예약 현황</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background:#1a1a1a; color:#e0e0e0; font-family:Arial,sans-serif; padding:20px; }
    .card-dark { background:#2d2d2d; border:none; border-radius:8px; margin-bottom:20px; padding:20px; }
    .card-dark h5 { color:#fff; border-bottom:2px solid #4CAF50; padding-bottom:8px; margin-bottom:16px; }
    .available { color:#4CAF50; font-weight:600; }
    .unavail   { color:#f44336; }
    table { color:#e0e0e0; }
    th    { background:#363636 !important; color:#fff !important; border-color:#555 !important; }
    td    { border-color:#444 !important; }
    .btn-refresh { background:#4CAF50; border:none; color:#fff; }
    .btn-refresh:hover { background:#43a047; color:#fff; }
    .btn-city { border:2px solid #555; background:#363636; color:#ccc; padding:8px 24px; font-size:1rem; }
    .btn-city.active { background:#4CAF50; color:#fff; border-color:#4CAF50; }
    .btn-view { border:1px solid #555; background:#363636; color:#ccc; }
    .btn-view.active { background:#4CAF50; color:#fff; border-color:#4CAF50; }
    .spinner-border { color:#4CAF50; }
    .text-muted { color:#888 !important; }
    /* Yongin Calendar */
    .cal-wrap { overflow-x:auto; }
    .cal-table { border-collapse:collapse; min-width:100%; }
    .cal-table th, .cal-table td { border:1px solid #444; padding:4px 6px; white-space:nowrap; font-size:0.8rem; vertical-align:top; }
    .cal-table .col-court { background:#363636; color:#ddd; min-width:80px; max-width:100px; white-space:normal; word-break:keep-all; position:sticky; left:0; z-index:1; }
    .cal-table .col-date  { background:#2a2a2a; text-align:center; min-width:72px; }
    .cal-table .col-date.weekend { color:#ff8a65; }
    .cal-table .col-date.today   { background:#1b3a1b; color:#4CAF50; font-weight:bold; }
    .slot-avail { background:#1b3a1b; color:#4CAF50; border-radius:3px; padding:1px 4px; margin:1px 0; display:block; font-size:0.75rem; }
    .slot-taken { background:#3a1b1b; color:#888; border-radius:3px; padding:1px 4px; margin:1px 0; display:block; font-size:0.75rem; }
    .no-slot    { color:#555; font-size:0.75rem; }
    .nav-tabs .nav-link { color:#aaa; border-color:#555 #555 #2d2d2d; }
    .nav-tabs .nav-link.active { background:#2d2d2d; color:#4CAF50; border-color:#555 #555 #2d2d2d; }
    .nav-tabs { border-bottom-color:#555; }
    /* Sungnam */
    .sn-date-header { font-size:1rem; color:#4CAF50; border-bottom:1px solid #444; padding:4px 0; margin:12px 0 6px; }
    .sn-facility { font-weight:bold; margin:8px 0 2px; }
    .sn-slot { margin-left:16px; color:#4CAF50; }
    .facility-section { margin-bottom:16px; }
    .facility-header { background:#363636; padding:10px 14px; cursor:pointer; border-radius:4px; display:flex; justify-content:space-between; align-items:center; color:#fff; }
    .facility-header:hover { background:#404040; }
    .date-section { margin:8px 0; }
    .date-header { background:#404040; padding:7px 12px; cursor:pointer; border-radius:4px; display:flex; justify-content:space-between; align-items:center; color:#ddd; }
    .date-header:hover { background:#4a4a4a; }
    .facility-content, .date-content { padding:10px; background:#363636; border-radius:0 0 4px 4px; }
    .toggle-icon { font-size:12px; color:#888; }
    .status-available { color:#4CAF50; font-weight:bold; }
    .status-reserved  { color:#f44336; }
    .top-section-header { background:#3a3a3a; padding:10px 16px; cursor:pointer; border-radius:6px; display:flex; justify-content:space-between; align-items:center; color:#fff; font-weight:bold; margin-bottom:4px; }
    .top-section-header:hover { background:#484848; }
    .top-section-content { padding:8px 2px; }
  </style>
</head>
<body>
<div class="container-fluid" style="max-width:1400px">

  <!-- 헤더 -->
  <div class="card-dark text-center">
    <h2 class="mb-2">🎾 테니스 코트 예약 현황</h2>
    <div class="d-flex justify-content-center gap-2 mb-3">
      <button class="btn btn-city active" id="btn-sungnam" onclick="switchCity('sungnam')">🏙 성남</button>
      <button class="btn btn-city"        id="btn-yongin"  onclick="switchCity('yongin')">🏙 용인</button>
    </div>
    <div class="text-muted small">
      마지막 갱신: <span id="lastUpdate">-</span>
      &nbsp;|&nbsp;<span id="periodInfo"></span>
    </div>
    <button class="btn btn-refresh btn-sm mt-2" onclick="doRefresh()">🔄 새로고침</button>
  </div>

  <!-- ─── 성남 뷰 ─── -->
  <div id="city-sungnam">
    <div class="card-dark">
      <div class="top-section">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>⭐ 예약 가능한 관심 코트</span><span class="toggle-icon">▲</span>
        </div>
        <div class="top-section-content">
          <div id="sn-interestDiv"><div class="text-center"><div class="spinner-border"></div></div></div>
        </div>
      </div>
      <div class="top-section mt-3">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>✅ 예약 가능한 모든 코트</span><span class="toggle-icon">▼</span>
        </div>
        <div class="top-section-content" style="display:none">
          <div id="sn-availDiv"></div>
        </div>
      </div>
      <div class="top-section mt-3">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>📊 전체 코트 현황</span><span class="toggle-icon">▼</span>
        </div>
        <div class="top-section-content" style="display:none">
          <div id="sn-tableDiv"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ─── 용인 뷰 ─── -->
  <div id="city-yongin" style="display:none">
    <ul class="nav nav-tabs mb-0 px-1" id="yn-tabRow">
      <li class="nav-item">
        <button class="nav-link" id="yn-tab-list" onclick="yn_switchTab('list')">📋 목록 뷰</button>
      </li>
      <li class="nav-item">
        <button class="nav-link active" id="yn-tab-cal" onclick="yn_switchTab('cal')">📅 달력 뷰</button>
      </li>
    </ul>

    <!-- 목록 뷰 -->
    <div id="yn-view-list" class="card-dark" style="border-radius:0 8px 8px 8px; display:none;">
      <div class="top-section">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>⭐ 예약 가능한 관심 코트</span><span class="toggle-icon">▲</span>
        </div>
        <div class="top-section-content">
          <div id="yn-interestDiv"><div class="text-center"><div class="spinner-border"></div></div></div>
        </div>
      </div>
      <div class="top-section mt-3">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>✅ 예약 가능한 모든 코트</span><span class="toggle-icon">▼</span>
        </div>
        <div class="top-section-content" style="display:none">
          <div id="yn-availDiv"></div>
        </div>
      </div>
      <div class="top-section mt-3">
        <div class="top-section-header" onclick="toggleSection(this)">
          <span>📊 전체 코트 현황</span><span class="toggle-icon">▼</span>
        </div>
        <div class="top-section-content" style="display:none">
          <div id="yn-tableDiv"></div>
        </div>
      </div>
    </div>

    <!-- 달력 뷰 -->
    <div id="yn-view-cal" class="card-dark" style="border-radius:0 8px 8px 8px;">
      <h5>📅 달력 뷰 <small class="text-muted fs-6">(초록=예약가능, 빨강=마감)</small></h5>
      <div id="yn-areaFilter" class="mb-3 d-flex flex-wrap gap-2"></div>
      <div class="cal-wrap">
        <div id="yn-calDiv"><div class="text-center"><div class="spinner-border"></div></div></div>
      </div>
    </div>
  </div>

</div><!-- /container -->

<script>
// ─── 공통 상태 ────────────────────────────────────────────
var _city    = 'sungnam';
var _yn_tab  = 'cal';
var _yn_area = null;
var _yn_all  = [];
var _yn_avail = [];

// ─── 도시 전환 ───────────────────────────────────────────
function switchCity(city) {
  _city = city;
  document.getElementById('city-sungnam').style.display = city === 'sungnam' ? '' : 'none';
  document.getElementById('city-yongin').style.display  = city === 'yongin'  ? '' : 'none';
  document.getElementById('btn-sungnam').classList.toggle('active', city === 'sungnam');
  document.getElementById('btn-yongin').classList.toggle('active',  city === 'yongin');
  doRefresh();
}

function doRefresh() {
  if (_city === 'sungnam') sn_refresh();
  else yn_refresh();
}

// ══════════════════════════════════════════════════════════
// 성남 렌더링
// ══════════════════════════════════════════════════════════
function sn_refresh() {
  ['sn-interestDiv','sn-availDiv','sn-tableDiv'].forEach(function(id) {
    document.getElementById(id).innerHTML = '<div class="text-center"><div class="spinner-border"></div></div>';
  });
  fetch('/api/sungnam').then(function(r){ return r.json(); }).then(function(data) {
    document.getElementById('lastUpdate').textContent = data.last_update || '-';
    document.getElementById('periodInfo').textContent = '오늘 ~ +3일';
    sn_renderInterest(data.available   || []);
    sn_renderAvail(data.all_courts     || []);
    sn_renderTable(data.all_courts     || []);
  }).catch(function(e){ console.error(e); });
}

function toggleSection(el) {
  var content = el.nextElementSibling;
  var icon    = el.querySelector('.toggle-icon');
  if (content.style.display === 'none') {
    content.style.display = 'block'; icon.textContent = '▲';
  } else {
    content.style.display = 'none';  icon.textContent = '▼';
  }
}

// 1. 예약 가능한 관심 코트 (모니터링 시간대 매칭)
function sn_dowShort(dow) {
  var map = {'월요일':'월','화요일':'화','수요일':'수','목요일':'목','금요일':'금','토요일':'토','일요일':'일'};
  return map[dow] || dow;
}
function sn_renderInterest(avail) {
  var d = document.getElementById('sn-interestDiv');
  if (!avail.length) { d.innerHTML = '<div class="text-warning">예약 가능한 관심 코트가 없습니다.</div>'; return; }
  var byDate = {};
  var dowMap = {};
  avail.forEach(function(x) {
    dowMap[x.date] = x.day_of_week;
    if (!byDate[x.date]) byDate[x.date] = {};
    if (!byDate[x.date][x.facility_name]) byDate[x.date][x.facility_name] = [];
    byDate[x.date][x.facility_name].push(x);
  });
  var html = '';
  Object.keys(byDate).sort().forEach(function(dt) {
    var dow = sn_dowShort(dowMap[dt] || '');
    html += '<div class="sn-date-header">📅 ' + dt + '(' + dow + ')</div><div class="ms-2 mb-3">';
    Object.keys(byDate[dt]).sort().forEach(function(fn) {
      html += '<div class="sn-facility">🏟 ' + fn + '</div>';
      byDate[dt][fn].sort(function(a,b){ return a.time.localeCompare(b.time); }).forEach(function(s) {
        html += '<div class="sn-slot">✓ ' + s.court + ' &nbsp; ' + s.time + '</div>';
      });
    });
    html += '</div>';
  });
  d.innerHTML = html;
}

// 2. 예약 가능한 모든 코트 (시설별 토글)
function sn_renderAvail(courts) {
  var d = document.getElementById('sn-availDiv');
  var avail = courts.filter(function(x){ return x.is_available; });
  if (!avail.length) { d.innerHTML = '<div class="text-warning">예약 가능한 코트가 없습니다.</div>'; return; }
  var byFac = {};
  var dowMap = {};
  avail.forEach(function(x) {
    dowMap[x.date] = x.day_of_week;
    if (!byFac[x.facility_name]) byFac[x.facility_name] = {};
    if (!byFac[x.facility_name][x.date]) byFac[x.facility_name][x.date] = [];
    byFac[x.facility_name][x.date].push(x);
  });
  var html = '';
  // 탄천실내 제일 위
  var facs = Object.keys(byFac).sort(function(a,b){
    if (a === '탄천실내') return -1; if (b === '탄천실내') return 1;
    return a.localeCompare(b, 'ko');
  });
  facs.forEach(function(fn) {
    html += '<div class="facility-section"><div class="facility-header" onclick="toggleSection(this)">'
          + '<span>' + fn + '</span><span class="toggle-icon">▲</span></div>'
          + '<div class="facility-content">';
    Object.keys(byFac[fn]).sort().forEach(function(dt) {
      var dow = sn_dowShort(dowMap[dt] || '');
      html += '<div class="date-section"><div class="date-header" onclick="toggleSection(this)">'
            + '<span>' + dt + '(' + dow + ')</span><span class="toggle-icon">▲</span></div>'
            + '<div class="date-content"><table class="table table-sm mb-0"><thead><tr>'
            + '<th>코트</th><th>시간</th><th>상태</th></tr></thead><tbody>';
      byFac[fn][dt].sort(function(a,b){ return a.time.localeCompare(b.time); }).forEach(function(s) {
        html += '<tr><td>' + s.court + '</td><td>' + s.time + '</td><td class="status-available">예약 가능</td></tr>';
      });
      html += '</tbody></table></div></div>';
    });
    html += '</div></div>';
  });
  d.innerHTML = html;
}

// 3. 전체 코트 현황 (시설별 토글, 상태 포함)
function sn_renderTable(courts) {
  var d = document.getElementById('sn-tableDiv');
  if (!courts.length) { d.innerHTML = '<div class="text-muted">데이터 없음</div>'; return; }
  var byFac = {};
  var dowMap = {};
  courts.forEach(function(x) {
    dowMap[x.date] = x.day_of_week;
    if (!byFac[x.facility_name]) byFac[x.facility_name] = {};
    if (!byFac[x.facility_name][x.date]) byFac[x.facility_name][x.date] = [];
    byFac[x.facility_name][x.date].push(x);
  });
  var html = '';
  var facs = Object.keys(byFac).sort(function(a,b){
    if (a === '탄천실내') return -1; if (b === '탄천실내') return 1;
    return a.localeCompare(b, 'ko');
  });
  facs.forEach(function(fn) {
    html += '<div class="facility-section"><div class="facility-header" onclick="toggleSection(this)">'
          + '<span>' + fn + '</span><span class="toggle-icon">▲</span></div>'
          + '<div class="facility-content">';
    Object.keys(byFac[fn]).sort().forEach(function(dt) {
      var dow = sn_dowShort(dowMap[dt] || '');
      html += '<div class="date-section"><div class="date-header" onclick="toggleSection(this)">'
            + '<span>' + dt + '(' + dow + ')</span><span class="toggle-icon">▲</span></div>'
            + '<div class="date-content"><table class="table table-sm mb-0"><thead><tr>'
            + '<th>코트</th><th>시간</th><th>상태</th></tr></thead><tbody>';
      byFac[fn][dt].sort(function(a,b){ return a.time.localeCompare(b.time); }).forEach(function(s) {
        var statusCls  = s.is_available ? 'status-available' : 'status-reserved';
        var statusText = s.is_available ? '예약 가능'
                       : (s.reservation_name ? s.reservation_name + ' 님 예약' : '예약됨');
        html += '<tr><td>' + s.court + '</td><td>' + s.time
              + '</td><td class="' + statusCls + '">' + statusText + '</td></tr>';
      });
      html += '</tbody></table></div></div>';
    });
    html += '</div></div>';
  });
  d.innerHTML = html;
}

// ══════════════════════════════════════════════════════════
// 용인 렌더링
// ══════════════════════════════════════════════════════════
function yn_switchTab(tab) {
  _yn_tab = tab;
  document.getElementById('yn-view-list').style.display = tab === 'list' ? '' : 'none';
  document.getElementById('yn-view-cal').style.display  = tab === 'cal'  ? '' : 'none';
  document.getElementById('yn-tab-list').classList.toggle('active', tab === 'list');
  document.getElementById('yn-tab-cal').classList.toggle('active',  tab === 'cal');
  if (tab === 'cal' && _yn_all.length) yn_renderCal(_yn_all, _yn_area);
}

function yn_refresh() {
  ['yn-interestDiv','yn-availDiv','yn-tableDiv','yn-calDiv'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = '<div class="text-center"><div class="spinner-border"></div></div>';
  });
  fetch('/api/yongin').then(function(r){ return r.json(); }).then(function(data) {
    document.getElementById('lastUpdate').textContent = data.last_update || '-';
    document.getElementById('periodInfo').textContent = data.period || '';
    _yn_avail = data.available  || [];
    _yn_all   = data.all_courts || [];
    yn_renderInterest(_yn_avail);
    yn_renderAvail(_yn_all);
    yn_renderTable(_yn_all);
    yn_buildAreaFilter(_yn_all);
    if (_yn_tab === 'cal') yn_renderCal(_yn_all, _yn_area);
  }).catch(function(e){ console.error(e); });
}

function yn_shortName(n) {
  return n.replace(/\\[유료\\]|\\[무료\\]/g,'').replace(/_\\d{2}월$/,'').trim();
}
function yn_calName(n) {
  var clean = n.replace(/\\[유료\\]|\\[무료\\]/g,'').replace(/_\\d{2}월$/,'').trim();
  var idx = clean.indexOf('테니스장');
  if (idx < 0) return clean;
  var main = clean.slice(0, idx).trim();
  var suffix = clean.slice(idx + 4).trim();
  if (!suffix) return main;
  return main + '<br><small>' + suffix + '</small>';
}

function yn_buildAreaFilter(courts) {
  var areas = [];
  courts.forEach(function(x){ if (areas.indexOf(x.location) < 0) areas.push(x.location); });
  areas.sort();
  var wrap = document.getElementById('yn-areaFilter');
  wrap.innerHTML = '';
  var allBtn = document.createElement('button');
  allBtn.className = 'btn btn-sm ' + (!_yn_area ? 'btn-view active' : 'btn-view');
  allBtn.textContent = '전체';
  allBtn.onclick = function(){ yn_setArea(null); };
  wrap.appendChild(allBtn);
  areas.forEach(function(a) {
    var btn = document.createElement('button');
    btn.className = 'btn btn-sm ' + (_yn_area === a ? 'btn-view active' : 'btn-view');
    btn.textContent = a;
    btn.onclick = (function(area){ return function(){ yn_setArea(area); }; })(a);
    wrap.appendChild(btn);
  });
}

function yn_setArea(a) {
  _yn_area = a;
  yn_buildAreaFilter(_yn_all);
  yn_renderCal(_yn_all, a);
}

function yn_renderCal(courts, areaFilter) {
  var d = document.getElementById('yn-calDiv');
  if (!courts.length) { d.innerHTML = '<div class="text-muted">데이터 없음</div>'; return; }

  var dateSet = {};
  courts.forEach(function(x){ dateSet[x.date] = x.day_of_week; });
  var dates = Object.keys(dateSet).sort();

  var courtSet = {};
  courts.forEach(function(x) {
    if (areaFilter && x.location !== areaFilter) return;
    if (!courtSet[x.court_name]) courtSet[x.court_name] = x.location;
  });
  var courtNames = Object.keys(courtSet).sort();

  var slotMap = {};
  courts.forEach(function(x) {
    if (areaFilter && x.location !== areaFilter) return;
    if (!slotMap[x.court_name]) slotMap[x.court_name] = {};
    if (!slotMap[x.court_name][x.date]) slotMap[x.court_name][x.date] = [];
    slotMap[x.court_name][x.date].push(x);
  });

  var today  = new Date().toISOString().slice(0,10);
  var DOW_KO = ['일','월','화','수','목','금','토'];

  var html = '<table class="cal-table"><thead><tr><th class="col-court">코트</th>';
  dates.forEach(function(dt) {
    var d2  = new Date(dt + 'T00:00:00');
    var dow = d2.getDay();
    var cls = 'col-date' + (dt === today ? ' today' : '') + (dow === 0 || dow === 6 ? ' weekend' : '');
    html += '<th class="' + cls + '">' + dt.slice(5,7) + '/' + dt.slice(8,10)
          + '<br><span style="font-size:0.7rem">' + DOW_KO[dow] + '</span></th>';
  });
  html += '</tr></thead><tbody>';

  courtNames.forEach(function(name) {
    html += '<tr><td class="col-court">' + yn_calName(name)
          + '<br><small class="text-muted">' + courtSet[name].replace(', ', '<br>') + '</small></td>';
    dates.forEach(function(dt) {
      var slots = slotMap[name] ? (slotMap[name][dt] || []) : [];
      if (!slots.length) { html += '<td class="col-date"><span class="no-slot">-</span></td>'; return; }
      slots.sort(function(a,b){ return parseInt(a.time) - parseInt(b.time); });
      var cell = '';
      slots.forEach(function(s) {
        var t = s.time.replace(/ ~ .*/, '');
        cell += s.is_available
          ? '<span class="slot-avail">✓ ' + t + '</span>'
          : '<span class="slot-taken">✗ ' + t + '</span>';
      });
      html += '<td class="col-date">' + cell + '</td>';
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  d.innerHTML = html;
}

// 1. 예약 가능한 관심 코트 (모니터링 필터 통과 슬롯)
function yn_renderInterest(avail) {
  var d = document.getElementById('yn-interestDiv');
  if (!avail.length) { d.innerHTML = '<div class="text-warning">예약 가능한 관심 코트가 없습니다.</div>'; return; }
  var byDate = {};
  avail.forEach(function(x) {
    if (!byDate[x.date]) byDate[x.date] = {};
    if (!byDate[x.date][x.location]) byDate[x.date][x.location] = {};
    if (!byDate[x.date][x.location][x.court_name]) byDate[x.date][x.location][x.court_name] = [];
    byDate[x.date][x.location][x.court_name].push(x.time);
  });
  var dowMap = {};
  avail.forEach(function(x){ dowMap[x.date] = x.day_of_week; });
  var html = '';
  Object.keys(byDate).sort().forEach(function(dt) {
    html += '<div class="sn-date-header">📅 ' + dt + ' (' + (dowMap[dt] || '') + ')</div><div class="ms-2 mb-3">';
    Object.keys(byDate[dt]).sort().forEach(function(loc) {
      Object.keys(byDate[dt][loc]).sort().forEach(function(name) {
        html += '<div class="sn-facility">🏟 ' + yn_shortName(name) + '<small class="text-muted ms-2">' + loc + '</small></div>';
        byDate[dt][loc][name].sort().forEach(function(t) {
          html += '<div class="sn-slot">✓ ' + t + '</div>';
        });
      });
    });
    html += '</div>';
  });
  d.innerHTML = html;
}

// 2. 예약 가능한 모든 코트 (지역별 토글)
function yn_renderAvail(courts) {
  var d = document.getElementById('yn-availDiv');
  var avail = courts.filter(function(x){ return x.is_available; });
  if (!avail.length) { d.innerHTML = '<div class="text-warning">예약 가능한 코트가 없습니다.</div>'; return; }
  var byLoc = {};
  var dowMap = {};
  avail.forEach(function(x) {
    dowMap[x.date] = x.day_of_week;
    if (!byLoc[x.location]) byLoc[x.location] = {};
    if (!byLoc[x.location][x.date]) byLoc[x.location][x.date] = [];
    byLoc[x.location][x.date].push(x);
  });
  var html = '';
  Object.keys(byLoc).sort().forEach(function(loc) {
    html += '<div class="facility-section"><div class="facility-header" onclick="toggleSection(this)">'
          + '<span>' + loc + '</span><span class="toggle-icon">▲</span></div>'
          + '<div class="facility-content">';
    Object.keys(byLoc[loc]).sort().forEach(function(dt) {
      var dow = dowMap[dt] || '';
      html += '<div class="date-section"><div class="date-header" onclick="toggleSection(this)">'
            + '<span>' + dt + '(' + dow + ')</span><span class="toggle-icon">▲</span></div>'
            + '<div class="date-content"><table class="table table-sm mb-0"><thead><tr>'
            + '<th>코트</th><th>시간</th><th>상태</th></tr></thead><tbody>';
      byLoc[loc][dt].sort(function(a,b){ return a.time.localeCompare(b.time); }).forEach(function(s) {
        html += '<tr><td>' + yn_shortName(s.court_name) + '</td><td>' + s.time + '</td><td class="status-available">예약 가능</td></tr>';
      });
      html += '</tbody></table></div></div>';
    });
    html += '</div></div>';
  });
  d.innerHTML = html;
}

// 3. 전체 코트 현황 (지역별 토글)
function yn_renderTable(courts) {
  var d = document.getElementById('yn-tableDiv');
  if (!courts.length) { d.innerHTML = '<div class="text-muted">데이터 없음</div>'; return; }
  var byLoc = {};
  var dowMap = {};
  courts.forEach(function(x) {
    dowMap[x.date] = x.day_of_week;
    if (!byLoc[x.location]) byLoc[x.location] = {};
    if (!byLoc[x.location][x.date]) byLoc[x.location][x.date] = [];
    byLoc[x.location][x.date].push(x);
  });
  var html = '';
  Object.keys(byLoc).sort().forEach(function(loc) {
    html += '<div class="facility-section"><div class="facility-header" onclick="toggleSection(this)">'
          + '<span>' + loc + '</span><span class="toggle-icon">▲</span></div>'
          + '<div class="facility-content">';
    Object.keys(byLoc[loc]).sort().forEach(function(dt) {
      var dow = dowMap[dt] || '';
      html += '<div class="date-section"><div class="date-header" onclick="toggleSection(this)">'
            + '<span>' + dt + '(' + dow + ')</span><span class="toggle-icon">▲</span></div>'
            + '<div class="date-content"><table class="table table-sm mb-0"><thead><tr>'
            + '<th>코트</th><th>시간</th><th>상태</th></tr></thead><tbody>';
      byLoc[loc][dt].sort(function(a,b){ return a.time.localeCompare(b.time); }).forEach(function(s) {
        var statusCls  = s.is_available ? 'status-available' : 'status-reserved';
        var statusText = s.is_available ? '예약 가능' : '예약됨';
        html += '<tr><td>' + yn_shortName(s.court_name) + '</td><td>' + s.time
              + '</td><td class="' + statusCls + '">' + statusText + '</td></tr>';
      });
      html += '</tbody></table></div></div>';
    });
    html += '</div></div>';
  });
  d.innerHTML = html;
}

// ─── 초기화 ──────────────────────────────────────────────
switchCity('sungnam');         // 기본: 성남
setInterval(doRefresh, 60000); // 1분마다 현재 도시 갱신
// 백그라운드에서 양쪽 데이터 선 로딩
sn_refresh();
fetch('/api/yongin').then(function(r){ return r.json(); }).then(function(data){
  _yn_avail = data.available  || [];
  _yn_all   = data.all_courts || [];
}).catch(function(){});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────
# Flask 라우트
# ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return _TEMPLATE


@app.route("/api/sungnam")
def api_sungnam():
    with _lock:
        return jsonify({
            "available":   _sn_available,
            "all_courts":  _sn_courts,
            "last_update": _sn_last_update,
        })


@app.route("/api/yongin")
def api_yongin():
    with _lock:
        return jsonify({
            "available":   _yn_available,
            "all_courts":  _yn_courts,
            "last_update": _yn_last_update,
            "period":      _yn_period,
        })


# ─────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="성남+용인 테니스 코트 통합 모니터링")
    parser.add_argument("--port", type=int, default=8000, help="Flask 포트 (기본: 8000)")
    args = parser.parse_args()

    setup_logging()
    load_telegram_config()
    logging.info("=" * 60)
    logging.info("🎾 테니스 코트 통합 모니터링 시작")
    logging.info(f"   성남 설정: {SUNGNAM_DIR}")
    logging.info(f"   용인 설정: {YONGIN_DIR}")
    logging.info("=" * 60)

    # 백그라운드 모니터링 스레드 시작
    t_sn = threading.Thread(target=sungnam_loop, daemon=True, name="sungnam")
    t_yn = threading.Thread(target=yongin_loop,  daemon=True, name="yongin")
    t_sn.start()
    t_yn.start()

    logging.info(f"🌐 통합 웹 UI: http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)
