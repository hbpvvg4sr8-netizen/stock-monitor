#!/usr/bin/env python3
"""使用支持 Vision 的 OpenAI 模型分析 A 股技术图形。

环境变量：
    OPENAI_API_KEY   API 密钥（必填）
    OPENAI_API_BASE  API 根地址，默认 https://api.openai.com/v1
    OPENAI_MODEL     默认 gpt-6-luna

运行：
    python openai_vision_demo.py --code 000651 --name 格力电器
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import matplotlib.pyplot as plt


API_KEY = os.getenv("OPENAI_API_KEY")
API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")


def request_json(path: str, payload: dict) -> dict:
    if not API_KEY:
        raise RuntimeError("未设置 OPENAI_API_KEY")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{API_BASE}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "openai-vision-stock-demo/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API HTTP {exc.code}: {detail[:1200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI API 网络请求失败: {exc.reason}") from exc


def fetch_history(code: str, days: int = 160):
    import akshare as ak

    end = date.today()
    start = end - timedelta(days=days)
    frame = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="",
    )
    required = {"日期", "收盘", "成交量"}
    if not required.issubset(frame.columns) or frame.empty:
        raise RuntimeError(f"历史行情字段不足，实际字段: {list(frame.columns)}")
    return frame


def create_chart(frame, code: str, name: str, output: Path) -> None:
    frame = frame.copy()
    frame["日期"] = frame["日期"].astype(str)
    frame["收盘"] = frame["收盘"].astype(float)
    frame["成交量"] = frame["成交量"].astype(float)
    frame["MA20"] = frame["收盘"].rolling(20).mean()
    frame["MA60"] = frame["收盘"].rolling(60).mean()
    frame = frame.tail(120)

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, (price_ax, volume_ax) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    x = range(len(frame))
    price_ax.plot(x, frame["收盘"], label="Close", linewidth=1.8, color="#1f77b4")
    price_ax.plot(x, frame["MA20"], label="MA20", linewidth=1.2, color="#ff7f0e")
    price_ax.plot(x, frame["MA60"], label="MA60", linewidth=1.2, color="#2ca02c")
    # 使用英文图表标签，避免运行环境缺少中文字体导致模型看到方框字符。
    price_ax.set_title(f"Daily Technical Chart ({code})")
    price_ax.set_ylabel("Price")
    price_ax.legend(loc="upper left")

    volume_ax.bar(x, frame["成交量"], width=0.8, color="#999999")
    volume_ax.set_ylabel("Volume")
    volume_ax.set_xlabel("Date")
    ticks = list(range(0, len(frame), max(1, len(frame) // 8)))
    volume_ax.set_xticks(ticks)
    volume_ax.set_xticklabels([frame.iloc[i]["日期"] for i in ticks], rotation=30, ha="right")
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160, bbox_inches="tight")
    plt.close(fig)


def analyze_chart(image_path: Path, code: str, name: str, model: str) -> str:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    prompt = f"""你是技术分析辅助工具。请分析这张 {name}（{code}）A股日线技术图。
图中包含收盘价、MA20、MA60和成交量。请用中文简要输出：
1. 当前趋势（上升、下降或震荡）；
2. 收盘价与 MA20/MA60 的相对位置；
3. 是否存在明显的放量突破或放量跌破；
4. 关键支撑和压力区域（只能根据图形估计，并明确这是估计值）；
5. 风险提示。
只根据图像可见内容分析，不要编造新闻、财务数据或精确买卖建议。"""
    result = request_json(
        "/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是谨慎的中文金融图表分析助手。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
            "max_completion_tokens": 800,
        },
    )
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Vision API 返回格式异常: {result}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI Vision 股票技术图分析演示")
    parser.add_argument("--code", default="000651", help="股票代码")
    parser.add_argument("--name", default="格力电器", help="股票名称")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="支持 Vision 的模型")
    parser.add_argument("--output", type=Path, default=Path("runtime/stock_chart.png"))
    args = parser.parse_args()

    try:
        frame = fetch_history(args.code)
        create_chart(frame, args.code, args.name, args.output)
        print(f"技术图已生成：{args.output}")
        print(f"Vision 模型：{args.model}")
        print("分析结果：")
        print(analyze_chart(args.output, args.code, args.name, args.model))
    except Exception as exc:  # noqa: BLE001 - 演示脚本直接显示调用错误
        print(f"演示失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
