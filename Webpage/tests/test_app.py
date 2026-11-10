import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import app as webpage

TOKEN = "test-token"
SNAPSHOT = {
    "sungnam": {"available": [], "all_courts": [{"court": "1번 코트"}], "last_update": "t",
                "status": "ok", "error": "", "progress": {"completed": 1, "total": 1}, "coverage": []},
    "yongin": {"available": [], "all_courts": [], "last_update": "t", "period": "p", "status": "ok", "error": ""},
}


class WebpageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "WEBPAGE_INGEST_TOKEN": TOKEN, "STATE_FILE": os.path.join(self.directory.name, "state.json"),
            "STALE_SECONDS": "300",
        })
        self.env.start()
        webpage._snapshot.clear()
        self.client = webpage.app.test_client()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()
        webpage._snapshot.clear()

    def post(self, body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return self.client.post("/api/ingest", json=body if body is not None else {"snapshot": SNAPSHOT},
                                headers=headers)

    def test_index_and_health(self):
        self.assertIn("<html", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get("/healthz").get_data(as_text=True), "ok")

    def test_waiting_placeholder_before_first_push(self):
        data = self.client.get("/api/sungnam").get_json()
        self.assertEqual(data["status"], "waiting")
        self.assertEqual(data["error"], "")
        self.assertEqual(data["all_courts"], [])
        self.assertIn("period", self.client.get("/api/yongin").get_json())

    def test_ingest_requires_valid_token(self):
        self.assertEqual(self.post(token=None).status_code, 401)
        self.assertEqual(self.post(token="wrong").status_code, 401)
        self.assertEqual(self.client.get("/api/sungnam").get_json()["status"], "waiting")

    def test_ingest_disabled_without_configured_token(self):
        with patch.dict(os.environ, {"WEBPAGE_INGEST_TOKEN": ""}):
            self.assertEqual(self.post().status_code, 503)

    def test_ingest_rejects_malformed_snapshot(self):
        self.assertEqual(self.post({"snapshot": {"sungnam": {}}}).status_code, 400)
        self.assertEqual(self.post({"snapshot": "x"}).status_code, 400)
        self.assertEqual(self.post({"nothing": 1}).status_code, 400)

    def test_pushed_snapshot_is_served(self):
        self.assertEqual(self.post().status_code, 200)
        sungnam = self.client.get("/api/sungnam").get_json()
        self.assertEqual(sungnam["all_courts"], [{"court": "1번 코트"}])
        self.assertEqual(sungnam["status"], "ok")
        self.assertEqual(self.client.get("/api/yongin").get_json()["period"], "p")

    def test_old_snapshot_is_marked_stale_but_data_is_kept(self):
        self.post()
        with patch.object(webpage.time, "time", return_value=time.time() + 700):
            data = self.client.get("/api/sungnam").get_json()
        self.assertEqual(data["status"], "stale")
        self.assertIn("11분", data["error"])
        self.assertEqual(data["all_courts"], [{"court": "1번 코트"}])

    def test_snapshot_survives_restart(self):
        self.post()
        webpage._snapshot.clear()
        webpage._load_state()
        self.assertEqual(self.client.get("/api/sungnam").get_json()["all_courts"], [{"court": "1번 코트"}])

    def test_oversized_body_is_rejected(self):
        big = json.dumps({"snapshot": SNAPSHOT, "pad": "x" * (9 * 1024 * 1024)})
        response = self.client.post("/api/ingest", data=big, content_type="application/json",
                                    headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
