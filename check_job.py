"""查询岗位信息与本月打卡/报告完成情况。"""
import json

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
from util.HelperFunctions import get_current_month_info
from util.CryptoUtils import aes_encrypt
import time

cfg = ConfigManager("user/me.json")
client = ApiClient(cfg)

# --- 岗位信息 ---
try:
    client.fetch_internship_plan()
    job = client.get_job_info()
    print("===== 岗位信息 =====")
    if isinstance(job, dict):
        for k in ["jobName", "practiceCompanyName", "companyName", "postName",
                  "jobAddress", "salary", "startTime", "endTime", "content"]:
            if job.get(k):
                print(f"  {k}: {job.get(k)}")
        # 兜底：打印全部键
        print("  --- 完整字段 ---")
        print("  " + json.dumps({k: v for k, v in job.items()
                                 if isinstance(v, (str, int, float)) and v not in ("", None)},
                                ensure_ascii=False)[:1200])
except Exception as e:
    print("岗位查询失败:", e)

# --- 本月打卡记录 ---
rsp = client._post_request(
    "attendence/clock/v2/listSynchro",
    client._get_authenticated_headers(),
    {**get_current_month_info(), "t": aes_encrypt(str(int(time.time() * 1000)))},
)
records = rsp.get("data") or []
print(f"\n===== 本月打卡 {len(records)} 条 =====")
for r in records:
    print(f"  {r.get('createTime')}  {r.get('type')}  {r.get('state')}  {r.get('address')}")
