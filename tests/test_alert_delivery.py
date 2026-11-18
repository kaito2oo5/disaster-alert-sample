# -*- coding: utf-8 -*-
"""防災情報 自動配信サンプルのテスト（標準ライブラリのみ）

実行: python3 -m unittest -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alert_delivery as ad  # noqa: E402


def make_event(**overrides):
    event = {
        "id": "test-001",
        "type": "earthquake",
        "occurred_at": "2026-11-18T06:15:00+09:00",
        "hypocenter": "テスト震源",
        "depth_km": 10,
        "magnitude": 6.1,
        "max_intensity": 5.0,
        "max_intensity_label": "5弱",
    }
    event.update(overrides)
    return event


def run_quiet(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = func(*args, **kwargs)
    return result, out.getvalue()


class TestJudge(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(ad.is_target(make_event(max_intensity=5.0)))   # 5弱
        self.assertTrue(ad.is_target(make_event(max_intensity=7.0)))   # 7
        self.assertFalse(ad.is_target(make_event(max_intensity=4.0)))  # 4
        self.assertFalse(ad.is_target(make_event(type="tsunami")))

    def test_format_message(self):
        msg = ad.format_message(make_event())
        self.assertEqual(
            msg,
            "【地震情報】\n"
            "11月18日 06時15分ごろ、地震がありました。\n"
            "震源：テスト震源（深さ約10km）\n"
            "規模：M6.1\n"
            "最大震度：5弱",
        )


class TestValidate(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(ad.validate(make_event()))

    def test_invalid(self):
        broken = make_event()
        del broken["max_intensity_label"]
        cases = [
            broken,
            make_event(max_intensity="5弱"),
            make_event(max_intensity=True),
            make_event(occurred_at="きのう"),
            "not a dict",
        ]
        for event in cases:
            with self.subTest(event=event):
                self.assertIsNotNone(ad.validate(event))


class TestProcess(unittest.TestCase):
    def test_sample_feed(self):
        events = ad.load_feed(ad.FEED_PATH)
        sent = []
        delivered, _ = run_quiet(ad.process, events, channels=[sent.append])
        self.assertEqual(delivered, 2)
        self.assertIn("能登半島沖", sent[0])
        self.assertIn("熊本県熊本地方", sent[1])

    def test_invalid_event_is_skipped_and_rest_continues(self):
        events = [make_event(id="bad", max_intensity="5弱"), make_event(id="ok")]
        sent = []
        delivered, out = run_quiet(ad.process, events, channels=[sent.append])
        self.assertEqual(delivered, 1)
        self.assertIn("スキップ", out)

    def test_duplicate_is_delivered_once_but_update_is_delivered(self):
        first = make_event()
        update = make_event(max_intensity=5.5, max_intensity_label="5強")  # 同じIDの続報
        sent = []
        delivered, _ = run_quiet(ad.process, [first, dict(first), update], channels=[sent.append])
        self.assertEqual(delivered, 2)
        self.assertIn("5強", sent[1])

    def test_failing_channel_does_not_stop_others(self):
        def broken(_message):
            raise RuntimeError("down")

        sent = []
        delivered, out = run_quiet(
            ad.process, [make_event(id="1"), make_event(id="2")], channels=[broken, sent.append]
        )
        self.assertEqual(delivered, 2)
        self.assertEqual(len(sent), 2)
        self.assertIn("配信失敗（broken）", out)


class TestLog(unittest.TestCase):
    def test_log_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.txt"
            with mock.patch.object(ad, "LOG_PATH", log):
                ad.deliver_log("テスト")
            self.assertEqual(log.read_text(encoding="utf-8"), "テスト\n\n")


class _Handler(BaseHTTPRequestHandler):
    # status_codes を順に返すテスト用 Webhook サーバー
    status_codes = []
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.received.append(json.loads(body))
        code = _Handler.status_codes.pop(0) if _Handler.status_codes else 200
        self.send_response(code)
        self.end_headers()

    def log_message(self, *args):
        pass


class TestWebhook(unittest.TestCase):
    def setUp(self):
        _Handler.status_codes = []
        _Handler.received = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        patches = [
            mock.patch.dict(os.environ, {"ALERT_WEBHOOK_URL": self.url, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}),
            mock.patch.object(ad, "WEBHOOK_RETRY_WAIT_SEC", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_success(self):
        run_quiet(ad.deliver_webhook, "本文")
        self.assertEqual(_Handler.received, [{"text": "本文"}])

    def test_retry_on_server_error(self):
        _Handler.status_codes = [500, 503, 200]
        run_quiet(ad.deliver_webhook, "本文")
        self.assertEqual(len(_Handler.received), 3)

    def test_gives_up_after_max_attempts(self):
        _Handler.status_codes = [500, 500, 500]
        with self.assertRaises(urllib.error.HTTPError):
            run_quiet(ad.deliver_webhook, "本文")
        self.assertEqual(len(_Handler.received), ad.WEBHOOK_MAX_ATTEMPTS)

    def test_no_retry_on_client_error(self):
        _Handler.status_codes = [400]
        with self.assertRaises(urllib.error.HTTPError):
            run_quiet(ad.deliver_webhook, "本文")
        self.assertEqual(len(_Handler.received), 1)

    def test_rejects_non_http_url(self):
        with mock.patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "file:///etc/passwd"}):
            with self.assertRaises(ValueError):
                ad.deliver_webhook("本文")

    def test_skip_when_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            _, out = run_quiet(ad.deliver_webhook, "本文")
        self.assertIn("スキップ", out)
        self.assertEqual(_Handler.received, [])


if __name__ == "__main__":
    unittest.main()
