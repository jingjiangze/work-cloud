# -*- coding: utf-8 -*-
"""用 Xvfb + xdotool 产生**操作系统级**鼠标事件过易盾滑块。

为什么要这一层：CDP 合成的鼠标/触摸事件位置再准（闭环能做到 0.0px 误差），
Yidun 仍拒；它判的是事件流的细粒度特征（时间戳连续性/加速度）。
xdotool 通过 X 服务器注入的事件，与真实鼠标在应用看来无法区分。

流程：Xvfb 起虚拟屏 → chromium 有头 --app 模式（无工具栏，坐标 1:1）
      → CDP 负责导航/填表/读 DOM → xdotool 负责拖动 → CDP 校验结果。
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

import websocket

PORT = 9239
DISP = ":99"
LOGIN = ("https://passport.zhihuishu.com/login"
         "?service=https://onlineservice-api.zhihuishu.com/login/gologin")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def http(p, m="GET"):
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, p), method=m)
    with urllib.request.urlopen(r, timeout=10) as x:
        return json.loads(x.read().decode())


def cred():
    d = {}
    for line in open("/tmp/zhs_login.env", encoding="utf-8"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            d[k] = v
    return d["ZHS_USER"], d["ZHS_PASS"]


def xt(*args):
    """xdotool 包装，强制 DISPLAY。"""
    return sh("DISPLAY=%s xdotool %s" % (DISP, " ".join(str(a) for a in args)))


def main():
    u, pw = cred()
    sh("pkill -f 'Xvfb :99' ; pkill -f 'remote-debugging-port=%d'" % PORT)
    time.sleep(1)
    xv = subprocess.Popen(["Xvfb", DISP, "-screen", "0", "1280x900x24"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    env = dict(os.environ, DISPLAY=DISP)
    ch = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--app=" + LOGIN, "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--window-position=0,0",
         "--window-size=1280,900", "--disable-blink-features=AutomationControlled",
         "--remote-debugging-port=%d" % PORT, "--user-data-dir=/tmp/cdp-x"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                http("/json/version")
                break
            except Exception:
                pass
        page = None
        for _ in range(40):
            try:
                targets = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % PORT, timeout=5).read().decode())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if page:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not page:
            print("没有 page target")
            return 2
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=25,
                                         max_size=16 * 1024 * 1024)
        mid = [0]

        def send(m, pa=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": m, "params": pa or {}}))
            return mid[0]

        def call(m, pa=None, tm=25):
            i = send(m, pa)
            end = time.time() + tm
            while time.time() < end:
                x = json.loads(ws.recv())
                if x.get("id") == i:
                    return x.get("result") or {}
            return {}

        def ev(e):
            r = call("Runtime.evaluate", {"expression": e, "returnByValue": True})
            return (r.get("result") or {}).get("value")

        send("Page.enable")
        send("Runtime.enable")
        call("Page.navigate", {"url": LOGIN})
        time.sleep(6)
        print("webdriver:", ev("navigator.webdriver"), "| UA:", (ev("navigator.userAgent") or "")[:40])
        ev("""(()=>{const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(el.__proto__,'value').set;
            s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
            const m=document.querySelector('input[name=mobile]'),p=document.querySelector('input[type=password]');
            if(!m||!p) return 0; set(m,%s); set(p,%s);
            const cb=document.querySelector('input[type=checkbox]'); if(cb&&!cb.checked) cb.click(); return 1})()"""
           % (json.dumps(u), json.dumps(pw)))
        time.sleep(1)
        ev("(()=>{const b=document.querySelector('.btn-block__grandient_login'); if(b)b.click(); return 1})()")
        time.sleep(5)

        g = json.loads(ev("""JSON.stringify((()=>{const bg=document.querySelector('img.yidun_bg-img'),
            jg=document.querySelector('img.yidun_jigsaw'), sl=document.querySelector('.yidun_slider');
            if(!bg||!jg||!sl) return {ok:false};
            const rb=bg.getBoundingClientRect(), rj=jg.getBoundingClientRect(), rs=sl.getBoundingClientRect();
            return {ok:true,bg:bg.src,jg:jg.src,imgLeft:rb.x,sc:rb.width/bg.naturalWidth,
                    pieceX:rj.x,sx:rs.x+rs.width/2,sy:rs.y+rs.height/2,
                    vh:window.innerHeight,vw:window.innerWidth}})())""") or "{}")
        if not g.get("ok"):
            print("无滑块:", json.dumps(g)[:120])
            return 2
        print("视口:", g["vw"], "x", g["vh"], "| 滑块@(%.0f,%.0f)" % (g["sx"], g["sy"]))

        bg = urllib.request.urlopen(g["bg"], timeout=15).read()
        jg = urllib.request.urlopen(g["jg"], timeout=15).read()
        import cv2
        import numpy as np
        B = cv2.imdecode(np.frombuffer(bg, np.uint8), cv2.IMREAD_COLOR)
        J = cv2.imdecode(np.frombuffer(jg, np.uint8), cv2.IMREAD_UNCHANGED)
        a = J[:, :, 3]
        ys, xs = np.where(a > 10)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        r = cv2.matchTemplate(B[y0:y1 + 1, :, :3], J[y0:y1 + 1, x0:x1 + 1, :3],
                              cv2.TM_CCOEFF_NORMED)
        _, s1, _, loc = cv2.minMaxLoc(r)
        target = g["imgLeft"] + loc[0] * g["sc"]
        need = target - g["pieceX"]
        print("score=%.3f 目标=%.1f 需移动=%.1f" % (s1, target, need))
        if s1 < 0.5:
            print("匹配不可信")
            return 2

        x, y = int(g["sx"]), int(g["sy"])
        xt("mousemove", x - 4, y + 2)
        time.sleep(0.2)
        xt("mousemove", x, y)
        time.sleep(0.12)
        xt("mousedown", 1)
        time.sleep(random.uniform(0.15, 0.28))

        # 粗拖：手柄需比拼图多走约 17px
        drag = need + 17
        steps = 42
        for i in range(1, steps + 1):
            p = i / steps
            ease = 1 - (1 - p) ** 2.1
            xt("mousemove", int(x + drag * ease), int(y + random.uniform(-1, 1)))
            time.sleep(random.uniform(0.012, 0.026))
        time.sleep(0.35)

        # 闭环微调（OS 级）
        for it in range(5):
            cur = json.loads(ev("""JSON.stringify((()=>{const j=document.querySelector('img.yidun_jigsaw');
                return j?{x:j.getBoundingClientRect().x}:null})())""") or "{}")
            if not cur:
                break
            err = target - cur["x"]
            print("  闭环%d 拼图x=%.1f err=%.1f" % (it + 1, cur["x"], err))
            if abs(err) <= 1.2:
                break
            x = int(x + err)
            xt("mousemove", x, y)
            time.sleep(random.uniform(0.18, 0.3))
        time.sleep(random.uniform(0.1, 0.2))
        xt("mouseup", 1)

        ok = False
        for _ in range(12):
            time.sleep(0.8)
            s = json.loads(ev("""JSON.stringify({m:!!document.querySelector('.yidun_modal'),
                v:(document.querySelector('input[name=NECaptchaValidate]')||{}).value||''})""") or "{}")
            if (s.get("v") or "").strip():
                ok = True
                break
            if not s.get("m"):
                break
        url = ev("location.href") or ""
        print("通过=%s URL=%s" % (ok, url[:90]))
        if ok or "login.zhihuishu.com" not in url:
            ev("(()=>{const b=document.querySelector('.btn-block__grandient_login'); if(b)b.click(); return 1})()")
            time.sleep(6)
            url = ev("location.href") or ""
            print("最终 URL:", url[:100])
            ck = call("Network.getAllCookies", tm=15).get("cookies") or []
            jar = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                    "path": c.get("path", "/")} for c in ck]
            json.dump({"zhs": jar}, open("/tmp/zhs_cookies_new.json", "w", encoding="utf-8"),
                      ensure_ascii=False)
            print("cookie 导出 %d 条" % len(jar))
            return 0 if "login.zhihuishu.com" not in url else 1
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        ch.terminate()
        xv.terminate()


if __name__ == "__main__":
    sys.exit(main())
