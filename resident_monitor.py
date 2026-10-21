#!/usr/bin/env python3
"""A 股常驻盘中监控器。

工作日 09:30-11:30、13:00-15:00 每 3 分钟轮询自选股，监控：
- 当日跌幅；
- 成交量相对近期均值放大；
- 换手率过高；
- 20 日区间向上突破或向下跌破，并要求量能确认。

运行：
    python resident_monitor.py
    python resident_monitor.py --once --dry-run

规则配置：alert_rules.json；冷却状态：monitor_state.json。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dt_time
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
DEFAULT_RULES = {
    "daily_return_threshold_pct": -3.0,
    "volume_surge_ratio": 2.0,
    "turnover_rate_threshold_pct": 5.0,
    "breakout_buffer_pct": 0.5,
    "breakout_volume_ratio": 1.5,
    "technical_lookback_days": 20,
}


def now() -> datetime:
    return datetime.now(TZ)


def in_trading_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    current = moment.timetz().replace(tzinfo=None)
    return (MORNING_START <= current <= MORNING_END) or (
        AFTERNOON_START <= current <= AFTERNOON_END
    )


def load_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        merged = dict(default)
        merged.update(data)
        return merged
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"读取配置失败 {path}: {exc}") from exc


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
        headers={"Content-Type": "application/json", "User-Agent": "stock-monitor/1.0"},
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
                post_json(webhook, {"msgtype": "text", "text": {"content": message}})
            elif target == "wecom":
                webhook = os.getenv("WECOM_WEBHOOK")
                if not webhook:
                    raise RuntimeError("未设置 WECOM_WEBHOOK")
                post_json(webhook, {"msgtype": "text", "text": {"content": message}})
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
        except Exception as exc:  # noqa: BLE001
            success = False
            print(f"通知发送失败 [{target}]: {type(exc).__name__}: {exc}", file=sys.stderr)
    return success


def technical_metrics(quote: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """读取日线，计算 20 日区间和当前量能确认。失败时返回原因但不阻塞价格监控。"""
    try:
        import akshare as ak

        lookback = int(rules["technical_lookback_days"])
        end_date = now().date()
        start_date = end_date - timedelta(days=max(lookback * 5, 90))
        history = ak.stock_zh_a_hist(
            symbol=quote["code"],
            period="daily",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust="",
        )
        required = {"收盘", "最高", "最低", "成交量"}
        if not required.issubset(history.columns) or len(history) < lookback + 1:
            return {"technical_error": "历史日线不足，无法判断突破"}

        history = history.dropna(subset=list(required)).tail(lookback + 1)
        previous = history.iloc[:-1].tail(lookback)
        average_volume = float(previous["成交量"].astype(float).mean())
        current_volume = quote.get("volume")
        # AKShare 新浪实时成交量单位为股；历史成交量通常为手。
        if current_volume is not None and quote.get("source", "").startswith("AKShare"):
            current_volume = current_volume / 100
        current_volume = float(current_volume or 0)
        volume_surge = current_volume / average_volume if average_volume > 0 else None
        prior_high = float(previous["最高"].astype(float).max())
        prior_low = float(previous["最低"].astype(float).min())
        buffer_pct = float(rules["breakout_buffer_pct"])
        breakout_volume = float(rules["breakout_volume_ratio"])
        price = float(quote["price"])
        return {
            "technical_lookback_days": lookback,
            "average_volume_20d": average_volume,
            "volume_surge_ratio": volume_surge,
            "prior_high": prior_high,
            "prior_low": prior_low,
            "breakout_up": bool(
                price >= prior_high * (1 + buffer_pct / 100)
                and volume_surge is not None
                and volume_surge >= breakout_volume
            ),
            "breakdown_down": bool(
                price <= prior_low * (1 - buffer_pct / 100)
                and volume_surge is not None
                and volume_surge >= breakout_volume
            ),
        }
    except Exception as exc:  # noqa: BLE001 - 技术面失败不影响其他信号
        return {"technical_error": f"{type(exc).__name__}: {exc}"}


def build_signals(quote: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    daily_return = quote.get("daily_return_pct")
    if daily_return is not None and daily_return <= float(rules["daily_return_threshold_pct"]):
        signals.append(f"跌幅 {daily_return:.2f}%")

    turnover = quote.get("turnover_rate")
    if turnover is not None and turnover >= float(rules["turnover_rate_threshold_pct"]):
        signals.append(f"换手率 {turnover:.2f}%")

    metrics = quote.get("technical", {})
    surge = metrics.get("volume_surge_ratio")
    if surge is not None and surge >= float(rules["volume_surge_ratio"]):
        signals.append(f"成交量约为近{rules['technical_lookback_days']}日均值 {surge:.2f} 倍")
    elif quote.get("volume_ratio") is not None and quote["volume_ratio"] >= float(rules["volume_surge_ratio"]):
        signals.append(f"量比 {quote['volume_ratio']:.2f}")

    if metrics.get("breakout_up"):
        signals.append(f"向上突破近{rules['technical_lookback_days']}日高点")
    if metrics.get("breakdown_down"):
        signals.append(f"向下跌破近{rules['technical_lookback_days']}日低点")
    return signals


def format_alert(quote: dict[str, Any], signals: list[str]) -> str:
    metrics = quote.get("technical", {})
    extra = []
    if quote.get("turnover_rate") is not None:
        extra.append(f"换手率：{quote['turnover_rate']:.2f}%")
    if metrics.get("volume_surge_ratio") is not None:
        extra.append(f"量能/近期均值：{metrics['volume_surge_ratio']:.2f} 倍")
    return (
        f"A股监控预警｜{quote.get('name') or quote.get('requested_name')} ({quote['code']})\n"
        f"最新价：{quote['price']:.2f}\n"
        f"当日收益率：{quote.get('daily_return_pct'):.2f}%\n"
        f"触发信号：{'；'.join(signals)}\n"
        + ("；".join(extra) + "\n" if extra else "")
        + f"数据源：{quote.get('source')}｜行情时间：{quote.get('quote_time') or '未知'}"
    )


def run_once(
    watchlist_path: Path,
    state_path: Path,
    rules_path: Path,
    cooldown_minutes: int,
    dry_run: bool,
    threshold_override: float | None = None,
) -> dict[str, Any]:
    rules = load_json_file(rules_path, DEFAULT_RULES)
    if threshold_override is not None:
        rules["daily_return_threshold_pct"] = threshold_override
    result = monitor(load_watchlist(watchlist_path, None, None))
    state = load_state(state_path)
    current = now()
    cooldown_seconds = cooldown_minutes * 60
    alert_count = 0

    for quote in result["quotes"]:
        quote["technical"] = technical_metrics(quote, rules)
        signals = build_signals(quote, rules)
        quote["signals"] = signals
        code = quote["code"]
        if not signals:
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
            print(f"冷却中，跳过通知: {code} ({'；'.join(signals)})")
            continue

        alert_count += 1
        if send_notification(format_alert(quote, signals), dry_run=dry_run):
            state[code] = {
                "last_sent_at": current.isoformat(timespec="seconds"),
                "last_return_pct": quote.get("daily_return_pct"),
                "signals": signals,
            }

    save_state(state_path, state)
    result["summary"]["alert_count"] = alert_count
    result["summary"]["rules"] = rules
    print(json.dumps(result["summary"], ensure_ascii=False))
    return result


def seconds_to_next_tick(interval: int) -> float:
    epoch = time.time()
    remainder = epoch % interval
    return interval - remainder if remainder else interval


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股常驻盘中监控与多信号预警")
    parser.add_argument("--watchlist", type=Path, default=Path(os.getenv("WATCHLIST_FILE", "watchlist.json")))
    parser.add_argument("--rules", type=Path, default=Path(os.getenv("ALERT_RULES_FILE", "alert_rules.json")))
    parser.add_argument("--state-file", type=Path, default=Path(os.getenv("STATE_FILE", "monitor_state.json")))
    parser.add_argument("--threshold", type=float, default=None, help="临时覆盖跌幅阈值")
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
                run_once(args.watchlist, args.state_file, args.rules, args.cooldown, args.dry_run, args.threshold)
            except Exception as exc:  # noqa: BLE001 - 常驻进程记录错误后继续
                print(f"本轮轮询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            if args.once:
                return 0
        else:
            print(f"非交易时段，等待中: {current.isoformat(timespec='seconds')}")
        time.sleep(seconds_to_next_tick(args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
