"""只读查询：拉取本月全部打卡记录，不做任何打卡动作。"""
import json

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
from util.HelperFunctions import get_current_month_info
from util.CryptoUtils import aes_encrypt
import time


cfg = ConfigManager("user/me.json")
client = ApiClient(cfg)

url = "attendence/clock/v2/listSynchro"
headers = client._get_authenticated_headers()
data = {
    **get_current_month_info(),
    "t": aes_encrypt(str(int(time.time() * 1000))),
}
rsp = client._post_request(url, headers, data)
records = rsp.get("data") or []

print(f"本月打卡记录共 {len(records)} 条\n")
for i, r in enumerate(records):
    print(f"--- 第 {i+1} 条 ---")
    for k in ["createTime", "type", "state", "address", "lastDetailAddress",
              "latitude", "longitude", "isBeyondFence", "planId", "attendanceType"]:
        if k in r and r.get(k) not in (None, ""):
            print(f"  {k:20s}: {r.get(k)}")
    print()
