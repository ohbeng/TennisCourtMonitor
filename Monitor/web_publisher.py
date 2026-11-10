"""모니터가 수집한 스냅샷을 Webpage 서비스로 전송한다."""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

CHANGE_CHECK_SECONDS = 10    # 스냅샷 변경 확인 주기
HEARTBEAT_SECONDS = 60       # 변경이 없어도 이 주기마다 전송 (Webpage의 연결 상태 판단용)
REQUEST_TIMEOUT = 15

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def build_endpoint(url):
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Webpage 주소가 올바르지 않습니다: {url!r}")
    if parts.scheme != "https" and parts.hostname not in _LOCAL_HOSTS:
        # 인증 토큰이 평문으로 전송되지 않도록 원격 주소는 HTTPS만 허용한다.
        raise ValueError("Webpage 주소는 https:// 여야 합니다 (localhost 제외).")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/ingest"


class WebPublisher:
    def __init__(self, url, token, build_snapshot, *, check_seconds=CHANGE_CHECK_SECONDS,
                 heartbeat_seconds=HEARTBEAT_SECONDS):
        self.endpoint = build_endpoint(url)
        self._token = token
        self._build_snapshot = build_snapshot
        self._check_seconds = check_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._thread = None
        self._session = requests.Session()
        self._last_body = None
        self._last_sent = None
        self._healthy = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-publisher")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._session.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.publish_if_needed()
            except Exception as exc:
                logging.error("[WEB] 전송 준비 중 오류: %s", exc)
            self._stop.wait(self._check_seconds)

    def publish_if_needed(self, now=None):
        """변경됐거나 heartbeat 주기가 지났을 때만 전송하고 전송 여부를 반환한다."""
        now = now if now is not None else time.monotonic()
        body = json.dumps(self._build_snapshot(), ensure_ascii=False, sort_keys=True)
        changed = body != self._last_body
        due = self._last_sent is None or now - self._last_sent >= self._heartbeat_seconds
        if not (changed or due):
            return False
        if self._post(body):
            self._last_body, self._last_sent = body, now
            return True
        return False

    def _post(self, snapshot_body):
        payload = ('{"sent_at": %s, "snapshot": %s}'
                   % (json.dumps(datetime.now(timezone.utc).isoformat()), snapshot_body))
        try:
            response = self._session.post(
                self.endpoint, data=payload.encode("utf-8"), timeout=REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {self._token}",
                         "Content-Type": "application/json; charset=utf-8"},
            )
        except requests.RequestException as exc:
            self._report(False, f"Webpage 연결 실패: {exc.__class__.__name__}")
            return False
        if response.status_code == 200:
            self._report(True, "Webpage 전송 성공")
            return True
        if response.status_code in (401, 403):
            self._report(False, "Webpage가 토큰을 거부했습니다 (WEBPAGE_INGEST_TOKEN 확인).")
        else:
            self._report(False, f"Webpage 응답 오류: HTTP {response.status_code}")
        return False

    def _report(self, healthy, message):
        # 상태가 바뀔 때만 기록해 로그가 반복되지 않게 한다.
        if healthy != self._healthy:
            (logging.info if healthy else logging.error)("[WEB] %s", message)
        self._healthy = healthy
