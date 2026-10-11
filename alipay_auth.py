"""生成工学云「支付宝安全验证」授权链接，并把登记号写入配置。

用法：
    .venv/Scripts/python.exe alipay_auth.py

流程：
    1. 调 usercenter/alipay/v1/createAxdjk 拿到 outRegisterNo 与支付宝深链
    2. 把 outRegisterNo 写入 user/me.json 的 config.clockIn.outRegisterNo
    3. 把链接发到手机，在支付宝里完成授权
    4. 之后打卡会自动携带该凭据，触发 304 时用它重试

注意：登记号生成后需在支付宝完成授权才生效。
"""
import sys
import urllib.parse

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient


def build_links(register_url: str):
    """把 alipays:// 深链转成可直接发给手机的两种打开方式"""
    direct = register_url
    browser = "https://ds.alipay.com/?scheme=" + urllib.parse.quote(direct, safe="")
    return direct, browser


def main() -> int:
    cfg = ConfigManager("user/me.json")
    client = ApiClient(cfg)

    print("正在登录并申请支付宝安全验证登记...")
    result = client.create_alipay_clockin_verification()
    out_register_no = result["outRegisterNo"]
    register_url = result["registerUrl"]

    # 先落盘，授权完成后立即生效
    cfg.update_config(out_register_no, "config", "clockIn", "outRegisterNo")
    print(f"登记号已写入配置: {out_register_no}\n")

    direct, browser = build_links(register_url)

    print("=" * 60)
    print("请在手机上完成授权（二选一）：")
    print()
    print("  方式A（装了支付宝）：把下面这条链接发到手机微信/QQ，点击打开")
    print(f"  {browser}")
    print()
    print("  方式B：复制下面的深链，用手机浏览器打开")
    print(f"  {direct}")
    print("=" * 60)
    print()
    print("完成支付宝授权后，回复我一声，我立刻验证打卡是否通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
