#!/usr/bin/env python3
"""OpenAI-compatible API 演示。

环境变量：
    OPENAI_API_KEY   API 密钥（必填）
    OPENAI_API_BASE  API 根地址，默认 https://api.openai.com/v1
    OPENAI_MODEL     模型名，默认 gpt-6-luna

运行：
    python openai_demo.py
    python openai_demo.py --prompt "用三句话解释什么是换手率"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_KEY = os.getenv("OPENAI_API_KEY")
API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")


def request_json(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    if not API_KEY:
        raise RuntimeError("未设置 OPENAI_API_KEY")

    body = None
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "openai-api-demo/1.0",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(f"{API_BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API HTTP {exc.code}: {detail[:1000]}") from exc
    except URLError as exc:
        raise RuntimeError(f"API 网络请求失败: {exc.reason}") from exc


def list_models() -> list[str]:
    """展示模型目录，返回模型 ID 列表。"""
    result = request_json("/models")
    return [item["id"] for item in result.get("data", []) if "id" in item]


def chat(prompt: str, model: str) -> str:
    """调用 Chat Completions 接口完成一次对话。"""
    result = request_json(
        "/chat/completions",
        method="POST",
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个简洁、准确的中文助手。"},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": 300,
        },
    )
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"API 返回缺少 choices.message.content: {result}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI API 最小 Python 演示")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型 ID")
    parser.add_argument(
        "--prompt",
        default="请用三句话介绍你能如何帮助我分析一只 A 股股票。",
        help="发送给模型的问题",
    )
    parser.add_argument("--list-models", action="store_true", help="先输出可用模型列表")
    args = parser.parse_args()

    try:
        if args.list_models:
            print("可用模型：")
            for model in list_models():
                print(f"- {model}")
        print(f"\n请求模型：{args.model}")
        print("模型回复：")
        print(chat(args.prompt, args.model))
    except Exception as exc:  # noqa: BLE001 - 演示脚本直接展示 API 错误
        print(f"调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
