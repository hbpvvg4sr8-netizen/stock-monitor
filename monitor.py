
from datetime import datetime
from zoneinfo import ZoneInfo

stocks = {
    "688126": "沪硅产业",
    "920036": "觅睿科技"
}

print("股票监控系统启动")
print("北京时间:", datetime.now(ZoneInfo("Asia/Shanghai")))

for code, name in stocks.items():
    print(f"待监控: {name} ({code})")

print("初始化测试完成，尚未接入行情数据")
