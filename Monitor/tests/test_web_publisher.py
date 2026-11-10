import json
import unittest
from unittest.mock import Mock, patch

import requests

import web_publisher
from web_publisher import WebPublisher, build_endpoint


class EndpointTests(unittest.TestCase):
    def test_https_url_is_accepted_and_path_is_preserved(self):
        self.assertEqual(build_endpoint("https://x.onrender.com/"), "https://x.onrender.com/api/ingest")
        self.assertEqual(build_endpoint("https://x.example/app"), "https://x.example/app/api/ingest")

    def test_plain_http_is_allowed_only_for_localhost(self):
        self.assertEqual(build_endpoint("http://localhost:8000"), "http://localhost:8000/api/ingest")
        with self.assertRaises(ValueError):
            build_endpoint("http://example.com")
        with self.assertRaises(ValueError):
            build_endpoint("not a url")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"sungnam": {"status": "ok"}, "yongin": {"status": "ok"}}
        self.publisher = WebPublisher("https://x.example", "secret", lambda: self.snapshot, heartbeat_seconds=60)
        self.post = Mock(return_value=Mock(status_code=200))
        self.publisher._session = Mock(post=self.post)

    def test_posts_snapshot_with_bearer_token(self):
        self.assertTrue(self.publisher.publish_if_needed(now=0))
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://x.example/api/ingest")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        body = json.loads(kwargs["data"].decode("utf-8"))
        self.assertEqual(body["snapshot"], self.snapshot)
        self.assertIn("sent_at", body)

    def test_unchanged_snapshot_waits_for_heartbeat(self):
        self.publisher.publish_if_needed(now=0)
        self.assertFalse(self.publisher.publish_if_needed(now=30))
        self.assertTrue(self.publisher.publish_if_needed(now=61))
        self.assertEqual(self.post.call_count, 2)

    def test_changed_snapshot_is_sent_immediately(self):
        self.publisher.publish_if_needed(now=0)
        self.snapshot = {"sungnam": {"status": "blocked"}, "yongin": {"status": "ok"}}
        self.assertTrue(self.publisher.publish_if_needed(now=1))

    def test_failed_post_is_retried_on_next_check(self):
        self.post.side_effect = requests.ConnectionError("down")
        self.assertFalse(self.publisher.publish_if_needed(now=0))
        self.post.side_effect = None
        self.assertTrue(self.publisher.publish_if_needed(now=1))

    def test_rejected_token_is_not_marked_sent(self):
        self.post.return_value = Mock(status_code=401)
        self.assertFalse(self.publisher.publish_if_needed(now=0))
        self.post.return_value = Mock(status_code=200)
        self.assertTrue(self.publisher.publish_if_needed(now=1))

    def test_status_is_logged_only_when_it_changes(self):
        self.post.side_effect = requests.ConnectionError("down")
        with patch.object(web_publisher.logging, "error") as error:
            self.publisher.publish_if_needed(now=0)
            self.publisher.publish_if_needed(now=1)
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
