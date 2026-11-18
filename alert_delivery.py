#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防災情報 自動配信サンプル

地震情報フィードを読み込み、条件（最大震度）に合うものだけを
配信文に整形して、登録されたチャネルへ配信する。

実案件（気象庁XMLをWebSocket受信し、LINE / Discord へ24時間配信）の
処理の流れを、標準ライブラリのみで小さく再現したサンプル。

- 不正なデータ（必須項目の欠け・型違い）は配信せずにスキップする
- 同じ内容の情報が重複して届いても二重に配信しない（内容が更新された続報は配信する）
- 1つのチャネルが失敗しても、他のチャネル・後続の情報の配信は続ける
- Webhook は一時的な失敗に備えて再試行する
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 配信対象とする最大震度の下限（5弱 = 5.0 以上を配信）
INTENSITY_THRESHOLD = 5.0

# Webhook の再試行回数と待ち時間（秒）
WEBHOOK_MAX_ATTEMPTS = 3
WEBHOOK_RETRY_WAIT_SEC = 2

BASE_DIR = Path(__file__).parent
FEED_PATH = BASE_DIR / "sample_feed.json"
LOG_PATH = BASE_DIR / "delivery_log.txt"

# 必須項目と、受け付ける型
REQUIRED_FIELDS = {
    "id": str,
    "type": str,
    "occurred_at": str,
    "hypocenter": str,
    "depth_km": (int, float),
    "magnitude": (int, float),
    "max_intensity": (int, float),
    "max_intensity_label": str,
}


def load_feed(path):
    # 情報フィードを読み込む
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate(event):
    # 不正なデータなら理由を返す（問題なければ None）
    if not isinstance(event, dict):
        return "形式が不正です"
    for key, expected in REQUIRED_FIELDS.items():
        if key not in event:
            return f"必須項目 {key} がありません"
        value = event[key]
        if isinstance(value, bool) or not isinstance(value, expected):
            return f"{key} の型が不正です"
    try:
        datetime.fromisoformat(event["occurred_at"])
    except ValueError:
        return "occurred_at の日時形式が不正です"
    return None


def is_target(event):
    # 配信対象かどうかを判定する（validate 済みのデータを前提とする）
    return event["type"] == "earthquake" and event["max_intensity"] >= INTENSITY_THRESHOLD


def format_message(event):
    # 配信文に整形する
    occurred = datetime.fromisoformat(event["occurred_at"])
    lines = [
        "【地震情報】",
        f"{occurred.strftime('%m月%d日 %H時%M分')}ごろ、地震がありました。",
        f"震源：{event['hypocenter']}（深さ約{event['depth_km']}km）",
        f"規模：M{event['magnitude']}",
        f"最大震度：{event['max_intensity_label']}",
    ]
    return "\n".join(lines)


def deliver_console(message):
    print("--- 配信 ---")
    print(message)


def deliver_log(message):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(message + "\n\n")


def deliver_webhook(message):
    # Webhook URL が設定されていれば JSON で POST する（未設定時は送信しない）
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        print("（Webhook未設定のため送信をスキップしました）")
        return
    if not url.startswith(("https://", "http://")):
        raise ValueError("ALERT_WEBHOOK_URL は http(s) のURLを指定してください")
    data = json.dumps({"text": message}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                print(f"Webhook送信完了: HTTP {res.status}")
                return
        except urllib.error.HTTPError as e:
            # 4xx は送り直しても結果が変わらないので再試行しない
            if 400 <= e.code < 500:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
        if attempt < WEBHOOK_MAX_ATTEMPTS:
            print(f"Webhook送信失敗（{attempt}/{WEBHOOK_MAX_ATTEMPTS}回目）、再試行します")
            time.sleep(WEBHOOK_RETRY_WAIT_SEC)
    raise last_error


CHANNELS = [deliver_console, deliver_log, deliver_webhook]


def process(events, channels=None):
    # フィード全体を処理し、配信件数を返す
    channels = CHANNELS if channels is None else channels
    delivered = 0
    seen = set()
    for event in events:
        error = validate(event)
        if error:
            print(f"スキップ（{error}）: {event!r}"[:200])
            continue
        key = json.dumps(event, sort_keys=True, ensure_ascii=False)
        if key in seen:
            print(f"スキップ（配信済み）: {event['id']}")
            continue
        seen.add(key)
        if not is_target(event):
            print(f"対象外（震度{event['max_intensity_label']}）: {event['hypocenter']}")
            continue
        message = format_message(event)
        for channel in channels:
            try:
                channel(message)
            except Exception as e:
                # 1つのチャネルが失敗しても、他のチャネル・後続の地震の配信は続ける
                print(f"配信失敗（{channel.__name__}）: {e}")
        delivered += 1
    return delivered


def main():
    events = load_feed(FEED_PATH)
    delivered = process(events)
    print(f"完了: {len(events)}件中 {delivered}件を配信しました")


if __name__ == "__main__":
    main()
