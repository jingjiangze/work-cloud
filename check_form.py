"""打印周报问卷的完整选项与警戒值，便于选出安全答案。"""
import json

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
from util.CryptoUtils import aes_encrypt
import time

cfg = ConfigManager("user/me.json")
client = ApiClient(cfg)
client.login()
client.fetch_internship_plan()

url = "practice/paper/v2/info"
data = {"formType": 8, "t": aes_encrypt(str(int(time.time() * 1000)))}
rsp = client._post_request(url, client._get_authenticated_headers(), data)
fields = (rsp.get("data") or {}).get("formFieldDtoList") or []

for i, f in enumerate(fields):
    print(f"\n--- 第 {i+1} 项 | isMust={f.get('isMust')} ---")
    print(f"  题目: {f.get('title')}")
    print(f"  警戒值(warningSetValue): {f.get('warningSetValue')}")
    print(f"  fieldClass={f.get('fieldClass')}  fieldType={f.get('fieldType')}")
    opt = f.get("optionJson")
    if opt:
        try:
            opts = json.loads(opt) if isinstance(opt, str) else opt
            for o in opts:
                print(f"    选项 {o.get('opt')} -> {o.get('value') or o.get('name') or o}")
        except Exception:
            print("    原始:", opt)
    # 其他可能有用的键
    for k in ("fieldName", "placeholder", "tip", "remark"):
        if f.get(k):
            print(f"  {k}: {f.get(k)}")
