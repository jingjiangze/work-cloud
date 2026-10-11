"""强制用配置里的嘉祥坐标提交一次，验证异地坐标是否被风控拦截。
（绕过"今日已打卡"检查，只为拿到服务端真实响应）
"""
from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient

cfg = ConfigManager("user/me.json")
print("提交坐标:", cfg.get_value("config.clockIn.location.latitude"),
      cfg.get_value("config.clockIn.location.longitude"))
print("提交地址:", cfg.get_value("config.clockIn.location.address"))

client = ApiClient(cfg)
client.login()
client.fetch_internship_plan()

try:
    client.submit_clock_in({
        "type": "END",
        "lastDetailAddress": None,
        "attachments": None,
        "description": None,
    })
    print("\n>>> 结果：提交未报错，嘉祥坐标可被接受")
except Exception as e:
    print(f"\n>>> 结果：被拦截 —— {e}")
