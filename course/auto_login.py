# -*- coding: utf-8 -*-
"""知到登录自愈链路（三级 + 退避重试）。

为什么需要它：2026-10-29~11-03 连续 6 天 401 —— run_zhs.py 原来在 cookies 失效时直接
`sys.exit(1)`，既不重试也不通知，整条链路静默躺了 6 天。

三级顺序：
  L1 cookies 复验（getHikeList 才作数，页面级"登录成功"是假阳性）
  L2 账号密码（真浏览器 CAS 表单；需要 chromium 能出网，环境不满足时自动跳过）
  L3 扫码兜底：把二维码挂到 http://<PUBLIC_IP>/qr/<rand>.png 并推 PushPlus，
     轮询等待扫码成功 → 自动落 cookies → 继续跑

任何一级成功即返回 True；全部失败推一条明确的失败通知（含各级原因）。
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ZHS_DIR = "/opt/course-automation/src/fuckCourse/zhs"
FDIR = "/opt/course-automation/var/state/fuckCourse"
QR_PUB = "/var/www/qr"   # 必须是 nginx 能读的目录；/opt/course-automation 权限收紧过
CRED = "/opt/course-automation/etc/zhs-cred.env"
ENVF = "/opt/course-automation/etc/panel.env"
STATUS = "/tmp/zhs_autologin.txt"
ACC = os.environ.get("ZHS_ACC", "zh-8da374")
COOKIE_FILES = [FDIR + "/cookies-%s.json" % ACC, FDIR + "/cookies.json"]
QR_WAIT_SEC = int(os.environ.get("ZHS_QR_WAIT", "180"))


def log(msg):
    line = "[auto_login] %s" % msg
    print(line, flush=True)
    try:
        with open(STATUS, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _envfile(path):
    d = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def push(title, content):
    """课程通道的 PushPlus；拿不到 token 就只打日志。"""
    token = _envfile(ENVF).get("PUSHPLUS_TOKEN_COURSE") or _envfile(ENVF).get("PUSHPLUS_TOKEN")
    if not token:
        log("无 PushPlus token，跳过推送")
        return False
    try:
        import requests
        requests.get("https://www.pushplus.plus/send",
                     params={"token": token, "title": title, "content": content,
                             "template": "html"}, timeout=15)
        return True
    except Exception as exc:
        log("推送失败: %s" % exc)
        return False


# ---------- L1 ----------

def load_cookies(fucker):
    for p in COOKIE_FILES:
        if not os.path.exists(p):
            continue
        try:
            root = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        jar = root.get("zhs")
        if jar:
            try:
                fucker.cookies = jar
                return True
            except Exception:
                pass
    return False


def verify(fucker):
    try:
        fucker.getHikeList()
        return True
    except Exception as exc:
        log("getHikeList 失败: %s" % str(exc)[:110])
        return False


# ---------- L2 ----------

def password_login():
    """真浏览器 CAS 登录；本机 chromium 不能出网时快速失败。"""
    script = "/opt/course-automation/src/fuckCourse/pwd_login_browser.py"
    if not os.path.exists(script):
        return False
    py = "/opt/zhexam/venv/bin/python"
    if not os.path.exists(py):
        py = sys.executable
    try:
        env = dict(os.environ, ZHS_LOGIN_ATTEMPTS="1")   # 单次就够：失败要快速落到 L3
        r = subprocess.run([py, script], capture_output=True, text=True,
                           timeout=120, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        ok = "LOGIN_OK" in out
        log("密码登录: %s" % ("成功" if ok else out.strip().splitlines()[-1][:110] if out.strip() else "无输出"))
        return ok
    except Exception as exc:
        log("密码登录异常: %s" % str(exc)[:110])
        return False


# ---------- L3 ----------

def qr_login_and_wait():
    """扫码兜底：二维码挂公网 + 推送，轮询直到成功/超时。"""
    py = "/opt/zhexam/venv/bin/python"
    if not os.path.exists(py):
        py = sys.executable
    Path(QR_PUB).mkdir(parents=True, exist_ok=True)
    name = "%s.png" % secrets.token_hex(8)
    pub = Path(QR_PUB) / name
    tmpl = "/tmp/zhs_qr_%s.png" % secrets.token_hex(4)

    # qr_login.py 会把二维码写到 /tmp/zhs_qr.png，这里改成边跑边搬运
    script = "/opt/course-automation/src/fuckCourse/qr_login.py"
    if not os.path.exists(script):
        log("qr_login.py 不存在")
        return False
    try:
        proc = subprocess.Popen([py, script], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:
        log("拉起扫码进程失败: %s" % exc)
        return False

    base_ip = _envfile(ENVF).get("PUBLIC_IP") or "118.190.106.170"
    pushed = False
    deadline = time.time() + QR_WAIT_SEC
    try:
        while time.time() < deadline:
            time.sleep(3)
            if os.path.exists("/tmp/zhs_qr.png"):
                try:
                    shutil.copyfile("/tmp/zhs_qr.png", tmpl)
                    shutil.copyfile("/tmp/zhs_qr.png", str(pub))
                    os.chmod(str(pub), 0o644)
                except Exception:
                    pass
                if not pushed and pub.exists():
                    url = "http://%s/qr/%s" % (base_ip, name)
                    push("[行动] 知到登录已过期，请扫码",
                         "<p>自动化已连续失败，需要你扫一次码恢复会话（约 10 秒）。</p>"
                         "<p><a href=\"%s\">👉 点这里打开二维码</a></p>"
                         "<p>扫完不用管，脚本会自动继续。</p>" % url)
                    log("已推送二维码: %s" % url)
                    pushed = True
            st = Path("/tmp/zhs_qr_status.txt")
            if st.exists():
                s = st.read_text(encoding="utf-8", errors="ignore").strip()
                if s.startswith("ok"):
                    log("扫码成功: %s" % s[:80])
                    return True
                if s.startswith("error"):
                    log("扫码失败: %s" % s[:80])
                    return False
            if proc.poll() is not None:
                # 进程退出：看状态文件最终值
                if st.exists() and st.read_text(encoding="utf-8", errors="ignore").startswith("ok"):
                    return True
                log("扫码进程退出")
                return False
        log("扫码等待超时 %ds" % QR_WAIT_SEC)
        return False
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        for p in (str(pub), tmpl):
            try:
                os.remove(p)
            except Exception:
                pass


# ---------- 编排 ----------

def ensure_login(fucker):
    if load_cookies(fucker) and verify(fucker):
        log("L1 cookies 有效")
        return True
    log("L1 cookies 失效，进入 L2 密码登录")
    if password_login():
        fucker2 = _new_fucker()
        if load_cookies(fucker2) and verify(fucker2):
            _adopt(fucker, fucker2)
            log("L2 密码登录成功")
            return True
    log("L2 未成功，进入 L3 扫码兜底")
    if qr_login_and_wait():
        fucker3 = _new_fucker()
        if load_cookies(fucker3) and verify(fucker3):
            _adopt(fucker, fucker3)
            log("L3 扫码登录成功")
            return True
        log("L3 已扫码但复验失败")
    push("[失败] 知到登录自愈未成功",
         "<p>三级都失败：cookies 失效、密码登录不可用、扫码未完成。</p>"
         "<p>今天该跑的章节测试/问答未执行。</p>")
    return False


def _new_fucker():
    from fucker import Fucker
    return Fucker(tree_view=False, progressbar_view=False)


def _adopt(dst, src):
    try:
        dst.cookies = src.cookies
    except Exception:
        pass
