#!/usr/bin/env python3
"""多源 A 股行情测试：AKShare 主源，腾讯公开接口备用源。

安装 AKShare：
    pip install akshare pandas

运行：
    python multi_source_test.py
    python multi_source_test.py --code 000651 --name 格力电器
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def to_float(value: Any) -> float | None:
    """把空值、NaN 和带逗号的数字安全转换为 float。"""
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


def validate_quote(quote: dict[str, Any]) -> dict[str, Any]:
    """校验统一行情结构，价格无效时让主源进入备用源。"""
    price = to_float(quote.get("price"))
    if price is None or price <= 0:
        raise ValueError(f"无效最新价: {quote.get('price')!r}")

    quote["price"] = price
    for key in ("change", "change_pct", "open", "high", "low", "volume", "amount"):
        quote[key] = to_float(quote.get(key))
    return quote


def fetch_from_akshare(code: str) -> dict[str, Any]:
    """使用 AKShare 的新浪 A 股实时行情封装。"""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("未安装 AKShare，请执行: pip install akshare pandas") from exc

    frame = ak.stock_zh_a_spot()
    if "代码" not in frame.columns:
        raise ValueError(f"AKShare 返回缺少代码字段，实际字段: {list(frame.columns)}")

    # AKShare 新浪接口的代码有时带市场前缀，例如 sz000651。
    code_digits = code[-6:]
    code_col = frame["代码"].astype(str).str.extract(r"(\d{6})")[0]
    rows = frame[code_col == code_digits]
    if rows.empty:
        raise ValueError(f"AKShare 未找到股票代码 {code_digits}")

    row = rows.iloc[0]
    quote = {
        "source": "AKShare/Sina",
        "code": code_digits,
        "name": row.get("名称"),
        "price": row.get("最新价"),
        "change": row.get("涨跌额"),
        "change_pct": row.get("涨跌幅"),
        "open": row.get("今开"),
        "high": row.get("最高"),
        "low": row.get("最低"),
        "volume": row.get("成交量"),
        "amount": row.get("成交额"),
        "quote_time": row.get("时间戳"),
    }
    return validate_quote(quote)


def fetch_from_tencent(code: str) -> dict[str, Any]:
    """使用腾讯 qt.gtimg.cn 公开快照接口。"""
    code_digits = code[-6:]
    market = "sh" if code_digits.startswith(("5", "6", "9")) else "sz"
    symbol = f"{market}{code_digits}"
    url = "https://qt.gtimg.cn/q=" + urlencode({"": symbol})[1:]
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://stockapp.finance.qq.com/",
        },
    )
    with urlopen(request, timeout=8) as response:
        raw = response.read().decode("gbk", errors="replace")

    # 典型返回：v_sz000651="格力电器~000651~...";
    match = re.search(r'="(.*?)";', raw, re.S)
    if not match:
        raise ValueError(f"腾讯接口返回格式异常: {raw[:200]!r}")
    fields = match.group(1).split("~")
    if len(fields) < 38:
        raise ValueError(f"腾讯接口字段数量不足: {len(fields)}")

    # 腾讯公开接口的常用字段位置：1 名称，2 代码，3 最新价，4 昨收，
    # 5 今开，30 时间，31 涨跌额，32 涨跌幅，33 最高，34 最低，
    # 36 成交量（手），37 成交额（万元）。
    amount_wan = to_float(fields[37])
    quote = {
        "source": "Tencent qt.gtimg.cn",
        "code": fields[2] or code_digits,
        "name": fields[1],
        "price": fields[3],
        "change": fields[31],
        "change_pct": fields[32],
        "open": fields[5],
        "high": fields[33],
        "low": fields[34],
        "volume": fields[36],
        "amount": amount_wan * 10000 if amount_wan is not None else None,
        "amount_unit": "CNY",
        "quote_time": fields[30] if len(fields) > 30 else None,
    }
    return validate_quote(quote)


def fetch_with_fallback(code: str, name: str) -> dict[str, Any]:
    errors: list[str] = []
    for source_name, loader in (
        ("AKShare/Sina", fetch_from_akshare),
        ("Tencent", fetch_from_tencent),
    ):
        try:
            quote = loader(code)
            quote["requested_name"] = name
            quote["retrieved_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            return quote
        except Exception as exc:  # noqa: BLE001 - 测试脚本需要继续尝试备用源
            errors.append(f"{source_name}: {type(exc).__name__}: {exc}")

    raise RuntimeError("所有行情源均失败:\n" + "\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="AKShare + 腾讯 A 股多源行情测试")
    parser.add_argument("--code", default="000651", help="股票代码，默认 000651")
    parser.add_argument("--name", default="格力电器", help="股票名称")
    args = parser.parse_args()

    try:
        quote = fetch_with_fallback(args.code, args.name)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(quote, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
