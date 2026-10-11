"""把当前配置里的支付宝授权深链转成二维码，方便手机扫码。"""
import segno
import urllib.parse

from util.Config import ConfigManager

cfg = ConfigManager("user/me.json")
# 重新申请一次登记，保证链接与配置中的登记号一致
from coreApi.MainLogicApi import ApiClient

client = ApiClient(cfg)
result = client.create_alipay_clockin_verification()
out_no = result["outRegisterNo"]
register_url = result["registerUrl"]
cfg.update_config(out_no, "config", "clockIn", "outRegisterNo")

browser_url = "https://ds.alipay.com/?scheme=" + urllib.parse.quote(register_url, safe="")

qr = segno.make(browser_url, error="m")
out_png = r"C:\Users\Administrator\AutoMoGuDingCheckIn\logs\alipay_auth_qr.png"
qr.save(out_png, scale=9, border=3, dark="#000000", light="#FFFFFF")

print("登记号:", out_no)
print("二维码已生成:", out_png)
