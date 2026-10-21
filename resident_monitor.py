#!/usr/bin/env python3
"""A 股常驻盘中监控器。

功能：
- 工作日 09:30-11:30、13:00-15:00 每 3 分钟轮询 watchlist.json；
- 当日收益率低于阈值时触发通知；
- 通过本地 JSON 状态文件处理单只股票的通知冷却；
- 支持 console、钉钉、企业微信和 Telegram；
- --once / --dry-run 便于部署前测试。

环境变量：
    ALERT_THRESHOLD_PCT     触发阈值，默认 -3.0
    ALERT_COOLDOWN_MINUTES  冷却时间，默认 30
    POLL_INTERVAL_SECONDS   轮询间隔，默认 180
    WATCHLIST_FILE          自选股文件，默认 watchlist.json
    STATE_FILE              冷却状态文件，默认 monitor_state.json
    NOTIFY_CHANNEL          console/dingtalk/wecom/telegram/all，默认 console
    DINGTALK_WEBHOOK        钉钉完整机器人 Webhook
    WECOM_WEBHOOK           企业微信完整机器人 Webhook
    TELEGRAM_BOT_TOKEN      Telegram Bot Token
    TELEGRAM_CHAT_ID        Telegram Chat ID
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from multi_source_test import load_watchlist, monitor


TZ = ZoneInfo("Asia/Shanghai")
MORNING_START = dt_time(9, 30)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
AFTERNOON_END = dt_time(15, 0)


def now() -> datetime:
    return datetime.now(TZ)


def in_trading_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    current = moment.timetz().replace(tzinfo=None)
    return (MORNING_START <= current <= MORNING_END) or (
        AFTERNOON_START <= current <= AFTERNOON_END
    )


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"状态文件读取失败，将重新开始冷却计时: {exc}", file=sys.stderr)
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def post_json(url: str, payload: dict[str, Any], timeout: int = 10) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "stock-monitor/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")


def send_notification(message: str, dry_run: bool = False) -> bool:
    channel = os.getenv("NOTIFY_CHANNEL", "console").lower()
    if dry_run or channel == "console":
        print("[通知预览]\n" + message)
        return True

    channels = {"dingtalk", "wecom", "telegram"} if channel == "all" else {channel}
    success = True
    for target in channels:
        try:
            if target == "dingtalk":
                webhook = os.getenv("DINGTALK_WEBHOOK")
                if not webhook:
                    raise RuntimeError("未设置 DINGTALK_WEBHOOK")
                post_json(webhook, {
                    "msgtype": "text",
                    "text": {"content": message},
                })
            elif target == "wecom":
                webhook = os.getenv("WECOM_WEBHOOK")
                if not webhook:
                    raise RuntimeError("未设置 WECOM_WEBHOOK")
                post_json(webhook, {
                    "msgtype": "text",
                    "text": {"content": message},
                })
            elif target == "telegram":
                token = os.getenv("TELEGRAM_BOT_TOKEN")
                chat_id = os.getenv("TELEGRAM_CHAT_ID")
                if not token or not chat_id:
                    raise RuntimeError("未设置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
                post_json(
                    f"https://api.telegram.org/bot{quote(token, safe='')}/sendMessage",
                    {"chat_id": chat_id, "text": message},
                )
            else:
                raise RuntimeError(f"不支持的通知渠道: {target}")
            print(f"通知发送成功: {target}")
        except Exception as exc:  # noqa: BLE001 - 单渠道失败不阻塞其他渠道
            success = False
            print(f"通知发送失败 [{target}]: {type(exc).__name__}: {exc}", file=sys.stderr)
    return success


def format_alert(quote: dict[str, Any], threshold: float) -> str:
    return (
        f"A股跌幅预警｜{quote.get('name') or quote.get('requested_name')} "
        f"({quote['code']})\n"
        f"最新价：{quote['price']:.2f}\n"
        f"当日收益率：{quote['daily_return_pct']:.2f}%（阈值 {threshold:.2f}%）\n"
        f"数据源：{quote.get('source')}\n"
        f"行情时间：{quote.get('quote_time') or '未知'}"
    )


def run_once(
    watchlist_path: Path,
    state_path: Path,
    threshold: float,
    cooldown_minutes: int,
    dry_run: bool,
) -> dict[str, Any]:
    result = monitor(load_watchlist(watchlist_path, None, None))
    state = load_state(state_path)
    current = now()
    cooldown_seconds = cooldown_minutes * 60

    for quote in result["quotes"]:
        code = quote["code"]
        daily_return = quote.get("daily_return_pct")
        if daily_return is None:
            continue
        if daily_return > threshold:
            state.pop(code, None)
            continue

        previous = state.get(code, {})
        last_sent_at = previous.get("last_sent_at")
        last_sent_timestamp = 0.0
        if last_sent_at:
            try:
                last_sent_timestamp = datetime.fromisoformat(last_sent_at).timestamp()
            except ValueError:
                pass

        if current.timestamp() - last_sent_timestamp < cooldown_seconds:
            print(f"冷却中，跳过通知: {code} ({daily_return:.2f}%)")
            continue

        message = format_alert(quote, threshold)
        if send_notification(message, dry_run=dry_run):
            state[code] = {
                "last_sent_at": current.isoformat(timespec="seconds"),
                "last_return_pct": daily_return,
            }

    save_state(state_path, state)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return result


def seconds_to_next_tick(interval: int) -> float:
    epoch = time.time()
    remainder = epoch % interval
    return interval - remainder if remainder else interval


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股常驻盘中监控与通知冷却")
    parser.add_argument("--watchlist", type=Path, default=Path(os.getenv("WATCHLIST_FILE", "watchlist.json")) )
    parser.add_argument("--state-file", type=Path, default=Path(os.getenv("STATE_FILE", "monitor_state.json")))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("ALERT_THRESHOLD_PCT", "-3")))
    parser.add_argument("--cooldown", type=int, default=int(os.getenv("ALERT_COOLDOWN_MINUTES", "30")))
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "180")))
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--dry-run", action="store_true", help="只打印通知，不真正发送")
    args = parser.parse_args()

    if args.interval <= 0 or args.cooldown < 0:
        parser.error("--interval 必须大于 0，--cooldown 不能为负数")

    while True:
        current = now()
        if in_trading_session(current) or args.once:
            print(f"开始轮询: {current.isoformat(timespec='seconds')}")
            try:
                run_once(args.watchlist, args.state_file, args.threshold, args.cooldown, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - 常驻进程记录错误后继续
                print(f"本轮轮询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            if args.once:
                return 0
        else:
            print(f"非交易时段，等待中: {current.isoformat(timespec='seconds')}")

        time.sleep(seconds_to_next_tick(args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
