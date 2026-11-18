#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防災情報 自動配信サンプル

地震情報フィードを読み込み、条件（最大震度）に合うものだけを
配信文に整形して、登録されたチャネルへ配信する。

実案件（気象庁XMLをWebSocket受信し、LINE / Discord へ24時間配信）の
処理の流れを、標準ライブラリのみで小さく再現したサンプル。
"""

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

# 配信対象とする最大震度の下限（5弱 = 5.0 以上を配信）
INTENSITY_THRESHOLD = 5.0

BASE_DIR = Path(__file__).parent
FEED_PATH = BASE_DIR / "sample_feed.json"
LOG_PATH = BASE_DIR / "delivery_log.txt"


def load_feed(path):
    # 情報フィードを読み込む
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def is_target(event):
    # 配信対象かどうかを判定する
    return event.get("type") == "earthquake" and event.get("max_intensity", 0) >= INTENSITY_THRESHOLD


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
    # Webhook URL が設定されていれば JSON で POST する（未設定時は表示のみ）
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        print("（Webhook未設定のため送信をスキップしました）")
        return
    data = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        print(f"Webhook送信完了: HTTP {res.status}")


CHANNELS = [deliver_console, deliver_log, deliver_webhook]


def main():
    events = load_feed(FEED_PATH)
    delivered = 0
    for event in events:
        if not is_target(event):
            print(f"対象外（震度{event['max_intensity_label']}）: {event['hypocenter']}")
            continue
        message = format_message(event)
        for channel in CHANNELS:
            channel(message)
        delivered += 1
    print(f"完了: {len(events)}件中 {delivered}件を配信しました")


if __name__ == "__main__":
    main()
