"""查询已提交的周报/月报情况。"""
import json

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient

cfg = ConfigManager("user/me.json")
client = ApiClient(cfg)
client.login()
client.fetch_internship_plan()

for rtype, label in (("week", "周报"), ("month", "月报")):
    try:
        info = client.get_submitted_reports_info(rtype)
        submitted = info.get("data") or []
        flag = (info.get("flag") if isinstance(info, dict) else None)
        print(f"===== {label} =====")
        print(f"  待提交序号(flag+1): {(flag or 0) + 1}")
        print(f"  已提交 {len(submitted)} 篇")
        for r in submitted[:6]:
            print(f"    - {r.get('title') or r.get('weeks') or r.get('yearmonth')}"
                  f"  {r.get('createTime', '')}")
        print()
    except Exception as e:
        print(f"===== {label} 查询失败: {e} =====")
