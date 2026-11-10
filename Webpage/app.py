"""테니스 코트 현황 웹페이지 (Render 배포용).

수집은 하지 않는다. Monitor가 POST /api/ingest로 보낸 최신 스냅샷을 보관하고
브라우저에 대시보드와 JSON API를 제공한다.
"""

import hmac
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX_FILE = os.path.join(_HERE, "templates", "index.html")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

_lock = threading.Lock()
_snapshot = {}          # {"sungnam": {...}, "yongin": {...}, "received_at": epoch seconds}


def _ingest_token():
    return os.environ.get("WEBPAGE_INGEST_TOKEN", "").strip()


def _stale_seconds():
    try:
        return int(os.environ.get("STALE_SECONDS", "300"))
    except ValueError:
        return 300


def _state_file():
    return os.environ.get("STATE_FILE", os.path.join(tempfile.gettempdir(), "tennis_snapshot.json"))


def _save_state():
    # 재시작 직후 Monitor의 다음 전송이 오기 전까지 마지막 결과를 보여 주기 위한 최선 저장이다.
    path = _state_file()
    try:
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(_snapshot, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError as exc:
        logging.warning("스냅샷 저장 실패: %s", exc)


def _load_state():
    try:
        with open(_state_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and isinstance(data.get("sungnam"), dict) and isinstance(data.get("yongin"), dict):
        _snapshot.update(data)


def _placeholder(city):
    payload = {"available": [], "all_courts": [], "last_update": "", "status": "waiting", "error": ""}
    if city == "sungnam":
        payload.update(progress={"completed": 0, "total": 0}, coverage=[])
    else:
        payload["period"] = ""
    return payload


def _city_payload(city):
    with _lock:
        stored = _snapshot.get(city)
        received_at = _snapshot.get("received_at")
    payload = dict(stored) if stored else _placeholder(city)
    if received_at is not None:
        age = time.time() - received_at
        payload["received_at"] = datetime.fromtimestamp(received_at, timezone.utc).isoformat()
        if age > _stale_seconds():
            payload["status"] = "stale"
            payload["error"] = f"모니터에서 {int(age // 60)}분째 데이터를 받지 못했습니다. 마지막 수신 결과를 표시합니다."
    return payload


@app.route("/")
def index():
    with open(_INDEX_FILE, encoding="utf-8") as handle:
        return Response(handle.read(), mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/api/sungnam")
def api_sungnam():
    return jsonify(_city_payload("sungnam"))


@app.route("/api/yongin")
def api_yongin():
    return jsonify(_city_payload("yongin"))


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    expected = _ingest_token()
    if not expected:
        return jsonify({"error": "ingest disabled"}), 503
    header = request.headers.get("Authorization", "")
    supplied = header[len("Bearer "):] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True)
    snapshot = body.get("snapshot") if isinstance(body, dict) else None
    if (not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("sungnam"), dict)
            or not isinstance(snapshot.get("yongin"), dict)):
        return jsonify({"error": "invalid snapshot"}), 400
    with _lock:
        _snapshot["sungnam"] = snapshot["sungnam"]
        _snapshot["yongin"] = snapshot["yongin"]
        _snapshot["received_at"] = time.time()
        _save_state()
    return jsonify({"ok": True})


_load_state()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
