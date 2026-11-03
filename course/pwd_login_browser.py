# -*- coding: utf-8 -*-
"""知到密码登录（真浏览器版）：CAS 表单 + WAF JavaScript 只能由浏览器完成。

流程：
  1. Playwright 打开 passport 登录页
  2. 填账号/密码，点登录
  3. 若出现图片验证码（WAF/风控触发），截图交给 OCR（外部注入 solve(img_bytes)->str）
  4. 登录成功后导出 cookies 写入 cookies-<acc>.json（与扫码登录同一格式）
  5. 用 getHikeList() 复验：页面级"登录成功"是假阳性（2026-10-29~11-03 连续 401 教训）

凭据只从 /opt/course-automation/etc/zhs-cred.env 读，不写磁盘、不打印。
"""
import json
import os
import sys
import time
from pathlib import Path

ZHS = "/opt/course-automation/src/fuckCourse/zhs"
FDIR = "/opt/course-automation/var/state/fuckCourse"
CRED = "/opt/course-automation/etc/zhs-cred.env"
STATUS = "/tmp/zhs_pwd_status.txt"

sys.path.insert(0, ZHS)
os.environ.setdefault("FUCKCOURSE_CONFIG", FDIR + "/config.json")

BASE = "https://passport.zhihuishu.com"
SVC = "https://onlineservice-api.zhihuishu.com/login/gologin"
ACC = os.environ.get("ZHS_ACC", "zh-8da374")
TARGETS = [FDIR + "/cookies-%s.json" % ACC, FDIR + "/cookies.json"]
ATTEMPTS = int(os.environ.get("ZHS_LOGIN_ATTEMPTS", "3"))


def w(msg):
    try:
        Path(STATUS).write_text(msg, encoding="utf-8")
    except Exception:
        pass


def cred():
    d = {}
    for line in open(CRED, encoding="utf-8"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            d[k] = v
    return d.get("ZHS_USER", ""), d.get("ZHS_PASS", "")


def fix_owner(p):
    try:
        ref = os.stat(FDIR + "/config.json")
        os.chown(p, ref.st_uid, ref.st_gid)
        os.chmod(p, 0o600)
    except Exception:
        pass


def ocr_image(png_bytes):
    """图片验证码识别；没装 OCR 就返回 None（由调用方决定是否放弃）。"""
    try:
        import ddddocr  # type: ignore
    except Exception:
        return None
    try:
        return ddddocr.DdddOcr(show_ad=False).classification(png_bytes).strip()
    except Exception:
        return None


def main():
    user, pwd = cred()
    if not user or not pwd:
        w("error missing-cred")
        print("LOGIN_FAIL missing cred")
        return 2

    from playwright.sync_api import sync_playwright

    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        w("attempt %d" % attempt)
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                ctx = b.new_context(locale="zh-CN")
                pg = ctx.new_page()
                pg.goto(BASE + "/login?service=" + SVC, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(1200)
                pg.fill("#lUsername", user)
                pg.fill("#lPassword", pwd)

                # 验证码（若该轮被要求）
                cap = ""
                try:
                    if pg.locator("#j-captcha-img").is_visible():
                        img = pg.locator("#j-captcha-img").screenshot()
                        cap = ocr_image(img) or ""
                        if cap:
                            pg.fill("#j-captcha-mobile", cap)
                except Exception:
                    pass

                clicked = False
                for sel in ("#f_sign_up button[type=submit]", "#f_sign_up .login-btn",
                            "#f_sign_up input[type=submit]", "button:has-text('登录')"):
                    try:
                        if pg.locator(sel).count() > 0:
                            pg.locator(sel).first.click()
                            clicked = True
                            break
                    except Exception:
                        pass
                if not clicked:
                    pg.keyboard.press("Enter")
                pg.wait_for_timeout(7000)

                url = pg.url
                ck = ctx.cookies()
                jar = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                        "path": c.get("path", "/")} for c in ck]
                ok_page = ("passport" not in url) and bool(jar)
                w("url=%s cookies=%d" % (url[:80], len(jar)))
                if ok_page:
                    for t in TARGETS:
                        Path(t).write_text(json.dumps({"zhs": jar}, ensure_ascii=False),
                                           encoding="utf-8")
                        fix_owner(t)
                    b.close()
                    print("LOGIN_OK attempt=%d url=%s cookies=%d" % (attempt, url[:60], len(jar)))
                    return 0
                b.close()
                last = "still-on-passport url=%s cap=%s" % (url[:70], cap or "-")
        except Exception as exc:
            last = str(exc)[:200]
        print("LOGIN_FAIL attempt=%d %s" % (attempt, last))
        w("fail attempt %d %s" % (attempt, last))
        if attempt < ATTEMPTS:
            time.sleep(5 * attempt)
    print("LOGIN_GIVEUP %s" % last)
    return 1


if __name__ == "__main__":
    sys.exit(main())
