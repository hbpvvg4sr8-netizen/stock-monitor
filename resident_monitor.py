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
Vision 配置：VISION_ENABLED、VISION_MODEL、VISION_OUTPUT_DIR。
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
    "intraday_periods": [30, 60],
    "intraday_lookback_bars": 20,
    "intraday_breakout_buffer_pct": 0.2,
    "intraday_breakout_volume_ratio": 1.5,
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "bollinger_period": 20,
    "bollinger_std": 2.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "kdj_period": 9,
    "kdj_signal": 3,
    "atr_period": 14,
    "atr_high_pct": 4.0,
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
    """读取日线并计算趋势、动量、波动率和量价指标。"""
    try:
        import akshare as ak
        import pandas as pd

        lookback = int(rules["technical_lookback_days"])
        end_date = now().date()
        indicator_warmup = max(
            int(rules["macd_slow"]) + int(rules["macd_signal"]) + 20,
            int(rules["bollinger_period"]),
            int(rules["atr_period"]),
            int(rules["kdj_period"]),
        )
        start_date = end_date - timedelta(days=max((lookback + indicator_warmup) * 4, 180))
        history_error = None
        try:
            history = ak.stock_zh_a_hist(
                symbol=quote["code"],
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
        except Exception as primary_exc:  # noqa: BLE001 - 使用腾讯历史日线兜底
            history_error = f"AKShare Eastmoney: {type(primary_exc).__name__}: {primary_exc}"
            market = "sz" if quote["code"].startswith(("0", "3")) else "sh"
            fallback = ak.stock_zh_a_hist_tx(
                symbol=f"{market}{quote['code']}",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
            history = fallback.rename(
                columns={
                    "date": "日期",
                    "close": "收盘",
                    "high": "最高",
                    "low": "最低",
                    "volume": "成交量",
                }
            )
        required = {"收盘", "最高", "最低", "成交量"}
        if not required.issubset(history.columns) or len(history) < lookback + 1:
            return {
                "technical_error": "历史日线不足，无法判断技术指标",
                "history_source_error": history_error,
            }

        history = history.dropna(subset=list(required)).copy()
        for column in required:
            history[column] = pd.to_numeric(history[column], errors="coerce")
        history = history.dropna(subset=list(required))
        close = history["收盘"].astype(float).copy()
        high = history["最高"].astype(float)
        low = history["最低"].astype(float)
        # 用盘中最新价替换末根日线收盘，避免指标滞后一根交易日。
        close.iloc[-1] = float(quote["price"])

        rsi_period = int(rules["rsi_period"])
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = (100 - (100 / (1 + rs))).fillna(100.0)

        macd_fast = int(rules["macd_fast"])
        macd_slow = int(rules["macd_slow"])
        macd_signal_period = int(rules["macd_signal"])
        ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=macd_signal_period, adjust=False).mean()
        macd_hist = macd_line - macd_signal

        bollinger_period = int(rules["bollinger_period"])
        bollinger_std = float(rules["bollinger_std"])
        bb_mid = close.rolling(bollinger_period).mean()
        bb_std = close.rolling(bollinger_period).std(ddof=0)
        bb_upper = bb_mid + bollinger_std * bb_std
        bb_lower = bb_mid - bollinger_std * bb_std

        kdj_period = int(rules["kdj_period"])
        lowest_low = low.rolling(kdj_period).min()
        highest_high = high.rolling(kdj_period).max()
        rsv = ((close - lowest_low) / (highest_high - lowest_low).replace(0, float("nan")) * 100).fillna(50)
        k = rsv.ewm(com=int(rules["kdj_signal"]) - 1, adjust=False).mean()
        d = k.ewm(com=int(rules["kdj_signal"]) - 1, adjust=False).mean()
        j = 3 * k - 2 * d

        atr_period = int(rules["atr_period"])
        previous_close = close.shift(1)
        true_range = pd.concat(
            [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.ewm(alpha=1 / atr_period, adjust=False).mean()

        def last(series: Any) -> float | None:
            value = series.iloc[-1]
            return float(value) if pd.notna(value) else None

        def prev_value(series: Any) -> float | None:
            value = series.iloc[-2]
            return float(value) if pd.notna(value) else None

        previous_window = history.iloc[:-1].tail(lookback)
        average_volume = float(previous_window["成交量"].astype(float).mean())
        current_volume = quote.get("volume")
        # AKShare 新浪实时成交量单位为股；历史成交量通常为手。
        if current_volume is not None and quote.get("source", "").startswith("AKShare"):
            current_volume = current_volume / 100
        current_volume = float(current_volume or 0)
        volume_surge = current_volume / average_volume if average_volume > 0 else None
        prior_high = float(previous_window["最高"].astype(float).max())
        prior_low = float(previous_window["最低"].astype(float).min())
        buffer_pct = float(rules["breakout_buffer_pct"])
        breakout_volume = float(rules["breakout_volume_ratio"])
        price = float(quote["price"])
        current_price = float(quote["price"])
        rsi_value = last(rsi)
        macd_value = last(macd_line)
        macd_signal_value = last(macd_signal)
        macd_hist_value = last(macd_hist)
        bb_upper_value = last(bb_upper)
        bb_mid_value = last(bb_mid)
        bb_lower_value = last(bb_lower)
        k_value, d_value, j_value = last(k), last(d), last(j)
        atr_value = last(atr)
        atr_pct = atr_value / current_price * 100 if atr_value is not None and current_price else None
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
            "rsi": rsi_value,
            "macd": macd_value,
            "macd_signal": macd_signal_value,
            "macd_histogram": macd_hist_value,
            "macd_bullish_cross": bool(
                prev_value(macd_line) is not None
                and prev_value(macd_signal) is not None
                and macd_value is not None
                and macd_signal_value is not None
                and prev_value(macd_line) <= prev_value(macd_signal)
                and macd_value > macd_signal_value
            ),
            "macd_bearish_cross": bool(
                prev_value(macd_line) is not None
                and prev_value(macd_signal) is not None
                and macd_value is not None
                and macd_signal_value is not None
                and prev_value(macd_line) >= prev_value(macd_signal)
                and macd_value < macd_signal_value
            ),
            "bollinger_upper": bb_upper_value,
            "bollinger_middle": bb_mid_value,
            "bollinger_lower": bb_lower_value,
            "bollinger_breakout_up": bool(
                bb_upper_value is not None and current_price > bb_upper_value
            ),
            "bollinger_breakdown_down": bool(
                bb_lower_value is not None and current_price < bb_lower_value
            ),
            "kdj_k": k_value,
            "kdj_d": d_value,
            "kdj_j": j_value,
            "kdj_golden_cross": bool(
                prev_value(k) is not None and prev_value(d) is not None
                and k_value is not None and d_value is not None
                and prev_value(k) <= prev_value(d) and k_value > d_value
            ),
            "kdj_dead_cross": bool(
                prev_value(k) is not None and prev_value(d) is not None
                and k_value is not None and d_value is not None
                and prev_value(k) >= prev_value(d) and k_value < d_value
            ),
            "atr": atr_value,
            "atr_pct": atr_pct,
            "history_source_fallback": history_error,
        }
    except Exception as exc:  # noqa: BLE001 - 技术面失败不影响其他信号
        return {"technical_error": f"{type(exc).__name__}: {exc}"}


def intraday_breakout_metrics(quote: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """检查 30/60 分钟线向上突破，并要求当前量能高于该周期均量。"""
    result: dict[str, Any] = {}
    try:
        import akshare as ak
        import pandas as pd

        lookback = int(rules["intraday_lookback_bars"])
        buffer_pct = float(rules["intraday_breakout_buffer_pct"])
        volume_threshold = float(rules["intraday_breakout_volume_ratio"])
        cutoff = now().replace(tzinfo=None)
        for period in rules.get("intraday_periods", [30, 60]):
            key = f"{int(period)}m"
            bars = ak.stock_zh_a_hist_min_em(
                symbol=quote["code"], period=str(period), adjust=""
            )
            required = {"时间", "收盘", "最高", "成交量"}
            if not required.issubset(bars.columns):
                result[key] = {"error": "分钟线缺少必要字段"}
                continue
            bars = bars.copy()
            bars["_time"] = pd.to_datetime(bars["时间"], errors="coerce")
            bars = bars[bars["_time"] <= cutoff].dropna(subset=list(required))
            if len(bars) < lookback + 1:
                result[key] = {"error": "分钟线数量不足"}
                continue
            bars = bars.tail(lookback + 1)
            previous = bars.iloc[:-1].tail(lookback)
            latest = bars.iloc[-1]
            average_volume = float(previous["成交量"].astype(float).mean())
            latest_volume = float(latest["成交量"])
            volume_surge = latest_volume / average_volume if average_volume > 0 else None
            prior_high = float(previous["最高"].astype(float).max())
            price = float(quote["price"])
            result[key] = {
                "bar_time": str(latest["时间"]),
                "prior_high": prior_high,
                "volume_surge_ratio": volume_surge,
                "breakout_up": bool(
                    price >= prior_high * (1 + buffer_pct / 100)
                    and volume_surge is not None
                    and volume_surge >= volume_threshold
                ),
            }
    except Exception as exc:  # noqa: BLE001 - 分钟线失败不影响日线和价格监控
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


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

    rsi = metrics.get("rsi")
    if rsi is not None and rsi <= float(rules["rsi_oversold"]):
        signals.append(f"RSI({rules['rsi_period']})超卖 {rsi:.1f}")
    elif rsi is not None and rsi >= float(rules["rsi_overbought"]):
        signals.append(f"RSI({rules['rsi_period']})超买 {rsi:.1f}")

    if metrics.get("macd_bullish_cross"):
        signals.append("MACD金叉")
    if metrics.get("macd_bearish_cross"):
        signals.append("MACD死叉")

    if metrics.get("bollinger_breakout_up"):
        signals.append("布林带上轨突破")
    if metrics.get("bollinger_breakdown_down"):
        signals.append("布林带下轨跌破")

    kdj_j = metrics.get("kdj_j")
    if metrics.get("kdj_golden_cross") and kdj_j is not None and kdj_j <= 50:
        signals.append(f"KDJ金叉 J={kdj_j:.1f}")
    if metrics.get("kdj_dead_cross") and kdj_j is not None and kdj_j >= 50:
        signals.append(f"KDJ死叉 J={kdj_j:.1f}")

    atr_pct = metrics.get("atr_pct")
    if atr_pct is not None and atr_pct >= float(rules["atr_high_pct"]):
        signals.append(f"ATR波动率偏高 {atr_pct:.2f}%")

    for period in rules.get("intraday_periods", [30, 60]):
        intraday = quote.get("intraday", {}).get(f"{int(period)}m", {})
        if not isinstance(intraday, dict):
            continue
        if intraday.get("breakout_up"):
            ratio = intraday.get("volume_surge_ratio")
            ratio_text = f"，量能约{ratio:.2f}倍" if ratio is not None else ""
            signals.append(f"{period}分钟线向上突破放量{ratio_text}")
    return signals


def vision_commentary(quote: dict[str, Any]) -> str | None:
    """为新预警生成一次技术图，并调用 Vision；失败时不阻塞原有报警。"""
    enabled = os.getenv("VISION_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
    if not enabled:
        return None
    if not os.getenv("OPENAI_API_KEY"):
        print("Vision 已跳过：未设置 OPENAI_API_KEY", file=sys.stderr)
        return None

    try:
        from openai_vision_demo import analyze_chart, create_chart, fetch_history

        output_dir = Path(os.getenv("VISION_OUTPUT_DIR", "runtime/vision"))
        chart_path = output_dir / f"{quote['code']}_{now().strftime('%Y%m%d_%H%M%S')}.png"
        frame = fetch_history(quote["code"])
        create_chart(frame, quote["code"], quote.get("name") or quote.get("requested_name", ""), chart_path)
        model = os.getenv("VISION_MODEL", "gpt-6-luna")
        commentary = analyze_chart(
            chart_path,
            quote["code"],
            quote.get("name") or quote.get("requested_name", ""),
            model,
        )
        print(f"Vision 分析完成: {quote['code']}，图表保存于 {chart_path}")
        return commentary[:3000]
    except Exception as exc:  # noqa: BLE001 - AI 失败不阻塞行情报警
        print(f"Vision 分析失败 [{quote['code']}]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def format_alert(
    quote: dict[str, Any], signals: list[str], vision: str | None = None
) -> str:
    metrics = quote.get("technical", {})
    extra = []
    if quote.get("turnover_rate") is not None:
        extra.append(f"换手率：{quote['turnover_rate']:.2f}%")
    if metrics.get("volume_surge_ratio") is not None:
        extra.append(f"量能/近期均值：{metrics['volume_surge_ratio']:.2f} 倍")
    for period, intraday in quote.get("intraday", {}).items():
        if not isinstance(intraday, dict):
            continue
        if intraday.get("volume_surge_ratio") is not None:
            extra.append(f"{period}线量能：{intraday['volume_surge_ratio']:.2f} 倍")
    if metrics.get("rsi") is not None:
        extra.append(f"RSI：{metrics['rsi']:.1f}")
    if metrics.get("macd_histogram") is not None:
        extra.append(f"MACD柱：{metrics['macd_histogram']:.3f}")
    if metrics.get("bollinger_middle") is not None:
        extra.append(
            f"布林带：{metrics['bollinger_lower']:.2f}/{metrics['bollinger_middle']:.2f}/{metrics['bollinger_upper']:.2f}"
        )
    if metrics.get("kdj_j") is not None:
        extra.append(f"KDJ J：{metrics['kdj_j']:.1f}")
    if metrics.get("atr_pct") is not None:
        extra.append(f"ATR/价格：{metrics['atr_pct']:.2f}%")
    message = (
        f"A股监控预警｜{quote.get('name') or quote.get('requested_name')} ({quote['code']})\n"
        f"最新价：{quote['price']:.2f}\n"
        f"当日收益率：{quote.get('daily_return_pct'):.2f}%\n"
        f"触发信号：{'；'.join(signals)}\n"
        + ("；".join(extra) + "\n" if extra else "")
        + f"数据源：{quote.get('source')}｜行情时间：{quote.get('quote_time') or '未知'}"
    )
    if vision:
        message += f"\n\nVision 技术图分析（辅助参考）：\n{vision}"
    return message


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
        quote["intraday"] = intraday_breakout_metrics(quote, rules)
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
        vision = vision_commentary(quote)
        if send_notification(format_alert(quote, signals, vision), dry_run=dry_run):
            state[code] = {
                "last_sent_at": current.isoformat(timespec="seconds"),
                "last_return_pct": quote.get("daily_return_pct"),
                "signals": signals,
            }

    save_state(state_path, state)
    for quote in result["quotes"]:
        signal_text = "；".join(quote.get("signals", [])) or "无触发信号"
        daily_return = quote.get("daily_return_pct")
        daily_text = f"{daily_return:.2f}%" if daily_return is not None else "未知"
        metrics = quote.get("technical", {})
        indicator_text = []
        if metrics.get("rsi") is not None:
            indicator_text.append(f"RSI={metrics['rsi']:.1f}")
        if metrics.get("macd_histogram") is not None:
            indicator_text.append(f"MACD柱={metrics['macd_histogram']:.3f}")
        if metrics.get("kdj_j") is not None:
            indicator_text.append(f"KDJ_J={metrics['kdj_j']:.1f}")
        if metrics.get("atr_pct") is not None:
            indicator_text.append(f"ATR={metrics['atr_pct']:.2f}%")
        print(
            f"行情 {quote.get('name') or quote.get('requested_name')}({quote['code']}) "
            f"价格={quote['price']:.2f} 当日涨跌={daily_text} "
            f"指标={' '.join(indicator_text) or '不可用'} 信号={signal_text}"
        )
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
