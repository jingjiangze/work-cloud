"""轻量推送：把人脸验证链接等即时消息直接发到微信（PushPlus）。

与主流程的 MessagePusher 不同，这里只做单条纯文本推送，
供"需要用户配合"的场景（如支付宝人脸验证）使用。
"""
import json
import os

import requests

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user", "me.json")


def get_pushplus_token() -> str:
    """从配置里读取 PushPlus 的 token"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for item in cfg.get("config", {}).get("pushNotifications", []):
            if item.get("type") == "PushPlus" and item.get("enabled"):
                return item.get("token", "")
    except Exception:
        pass
    return ""


def push_text(title: str, content: str, template: str = "txt") -> bool:
    """推送一条文本消息到微信。

    Args:
        title: 消息标题
        content: 消息正文
        template: PushPlus 模板，txt / html / markdown

    Returns:
        是否推送成功
    """
    token = get_pushplus_token()
    if not token:
        return False
    try:
        rsp = requests.post(
            "http://www.pushplus.plus/send",
            json={"token": token, "title": title,
                  "content": content, "template": template},
            timeout=15,
        )
        data = rsp.json() if rsp.content else {}
        return data.get("code") == 200
    except Exception:
        return False
