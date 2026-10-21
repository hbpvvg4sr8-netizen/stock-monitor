#!/usr/bin/env python3
"""多源 A 股自选股监控：AKShare 主源，腾讯公开接口备用源。

运行：
    pip install -r requirements.txt
    python multi_source_test.py
    python multi_source_test.py --watchlist watchlist.json
    python multi_source_test.py --code 000651 --name 格力电器
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_WATCHLIST = Path(__file__).with_name("watchlist.json")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in {"nan", "none", "-", "--"}:
            return None
        number = float(text)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_code(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 6:
        raise ValueError(f"股票代码必须是 6 位数字: {value!r}")
    return digits


def validate_quote(quote: dict[str, Any]) -> dict[str, Any]:
    price = to_float(quote.get("price"))
    if price is None or price <= 0:
        raise ValueError(f"无效最新价: {quote.get('price')!r}")

    quote["price"] = price
    for key in ("prev_close", "change", "change_pct", "open", "high", "low", "volume", "amount"):
        quote[key] = to_float(quote.get(key))

    if quote["prev_close"] and quote["prev_close"] > 0:
        quote["daily_return_pct"] = (price / quote["prev_close"] - 1) * 100
        quote["change"] = price - quote["prev_close"]
        quote["change_pct"] = quote["daily_return_pct"]
    elif quote["change_pct"] is not None:
        quote["daily_return_pct"] = quote["change_pct"]
    else:
        quote["daily_return_pct"] = None
    return quote


def load_watchlist(path: Path, code: str | None, name: str | None) -> list[dict[str, str]]:
    if code:
        return [{"code": normalize_code(code), "name": name or code}]

    with path.open(encoding="utf-8") as file:
        items = json.load(file)
    if not isinstance(items, list) or not items:
        raise ValueError("自选股清单必须是非空 JSON 数组")

    watchlist: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or "code" not in item:
            raise ValueError(f"自选股条目必须包含 code: {item!r}")
        watchlist.append({
            "code": normalize_code(item["code"]),
            "name": str(item.get("name") or item["code"]),
        })
    return watchlist


def fetch_akshare_batch(codes: list[str]) -> dict[str, dict[str, Any]]:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("未安装 AKShare，请执行: pip install -r requirements.txt") from exc

    frame = ak.stock_zh_a_spot()
    if "代码" not in frame.columns:
        raise ValueError(f"AKShare 返回缺少代码字段，实际字段: {list(frame.columns)}")

    code_col = frame["代码"].astype(str).str.extract(r"(\d{6})")[0]
    frame = frame.assign(_code=code_col)
    result: dict[str, dict[str, Any]] = {}
    for code in codes:
        rows = frame[frame["_code"] == code]
        if rows.empty:
            continue
        row = rows.iloc[0]
        try:
            result[code] = validate_quote({
                "source": "AKShare/Sina",
                "code": code,
                "name": row.get("名称"),
                "price": row.get("最新价"),
                "prev_close": row.get("昨收"),
                "change": row.get("涨跌额"),
                "change_pct": row.get("涨跌幅"),
                "open": row.get("今开"),
                "high": row.get("最高"),
                "low": row.get("最低"),
                "volume": row.get("成交量"),
                "amount": row.get("成交额"),
                "quote_time": row.get("时间戳"),
            })
        except ValueError:
            # 某只股票在盘前、停牌或上游异常时可能返回 0；其余股票仍可使用 AKShare。
            continue
    return result


def fetch_from_tencent(code: str) -> dict[str, Any]:
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    symbol = f"{market}{code}"
    request = Request(
        f"https://qt.gtimg.cn/q={symbol}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://stockapp.finance.qq.com/",
        },
    )
    with urlopen(request, timeout=8) as response:
        raw = response.read().decode("gbk", errors="replace")

    match = re.search(r'="(.*?)";', raw, re.S)
    if not match:
        raise ValueError(f"腾讯接口返回格式异常: {raw[:200]!r}")
    fields = match.group(1).split("~")
    if len(fields) < 38:
        raise ValueError(f"腾讯接口字段数量不足: {len(fields)}")

    amount_wan = to_float(fields[37])
    return validate_quote({
        "source": "Tencent qt.gtimg.cn",
        "code": fields[2] or code,
        "name": fields[1],
        "price": fields[3],
        "prev_close": fields[4],
        "change": fields[31],
        "change_pct": fields[32],
        "open": fields[5],
        "high": fields[33],
        "low": fields[34],
        "volume": fields[36],
        "amount": amount_wan * 10000 if amount_wan is not None else None,
        "amount_unit": "CNY",
        "quote_time": fields[30],
    })


def monitor(watchlist: list[dict[str, str]]) -> dict[str, Any]:
    codes = [item["code"] for item in watchlist]
    quotes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    try:
        ak_quotes = fetch_akshare_batch(codes)
        ak_error = None
    except Exception as exc:  # noqa: BLE001 - 失败后统一切换备用源
        ak_quotes = {}
        ak_error = f"{type(exc).__name__}: {exc}"

    for item in watchlist:
        code = item["code"]
        try:
            quote = ak_quotes.get(code)
            if quote is None:
                quote = fetch_from_tencent(code)
            quote["requested_name"] = item["name"]
            quotes.append(quote)
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响其他自选股
            failures.append({
                "code": code,
                "name": item["name"],
                "error": f"{type(exc).__name__}: {exc}",
            })

    returns = [q["daily_return_pct"] for q in quotes if q.get("daily_return_pct") is not None]
    summary = {
        "watchlist_count": len(watchlist),
        "success_count": len(quotes),
        "failure_count": len(failures),
        "equal_weight_daily_return_pct": sum(returns) / len(returns) if returns else None,
        "akshare_error": ak_error,
    }
    return {
        "retrieved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "summary": summary,
        "quotes": quotes,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AKShare + 腾讯备用的 A 股自选股监控")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="JSON 自选股清单")
    parser.add_argument("--code", help="临时只测试一只股票，优先于 --watchlist")
    parser.add_argument("--name", help="--code 对应的股票名称")
    args = parser.parse_args()

    try:
        watchlist = load_watchlist(args.watchlist, args.code, args.name)
        result = monitor(watchlist)
    except Exception as exc:
        print(f"监控失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 至少一个来源成功就保留有效输出；全部失败才让 Actions 失败。
    return 0 if result["summary"]["success_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
