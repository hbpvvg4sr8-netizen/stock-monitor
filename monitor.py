
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request, urlopen

STOCKS = {
    "格力电器": ("000651", "0.000651"),
    "沪硅产业": ("688126", "1.688126"),
    "觅睿科技": ("920036", "0.920036"),
}

URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

def fetch_kline(code, secid, period):
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": str(period),
        "fqt": "0",
        "beg": "20260901",
        "end": "20500101",
        "lmt": "100",
    }
    request = Request(
        URL + "?" + urlencode(params),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urlopen(request, timeout=20) as response:
        result = json.load(response)

    data = result.get("data") or {}
    if str(data.get("code")) != code:
        raise ValueError("股票代码不匹配或无数据")

    rows = data.get("klines") or []
    if not rows:
        raise ValueError("接口未返回K线")

    last = rows[-1].split(",")
    if len(last) < 7:
        raise ValueError("K线字段不完整")
    return last, len(rows)

now = datetime.now(ZoneInfo("Asia/Shanghai"))
print("股票行情接口测试")
print("北京时间:", now.isoformat(timespec="seconds"))

failures = 0

for name, (code, secid) in STOCKS.items():
    for period in (30, 60):
        print(f"\n{name} {code} | {period}分钟K线")
        try:
            row, count = fetch_kline(code, secid, period)
            print("成功获取:", count, "根K线")
            print("最后K线时间:", row[0])
            print("开盘:", row[1], "收盘:", row[2])
            print("最高:", row[3], "最低:", row[4])
            print("成交量(手):", row[5])
            print("成交额(元):", row[6])
        except Exception as exc:
            failures += 1
            print("获取失败:", type(exc).__name__, str(exc))

print("\n注意：最后一根K线可能尚未收盘；")
print("成功获取不代表行情实时或无延迟。")
print("失败项目数:", failures)

if failures:
    raise SystemExit(1)

