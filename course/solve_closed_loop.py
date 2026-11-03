# -*- coding: utf-8 -*-
"""闭环拖拽过易盾滑块。

关键：手柄位移与拼图位移不是 1:1（实测差约 17px 的固定 offset），
所以不追求"一次算准"，而是按住不放、读拼图实际位置、按误差微调，收敛后松手。
"""
import json
import random
import subprocess
import sys
import time
import urllib.request

import websocket

PORT = 9236
LOGIN = ("https://passport.zhihuishu.com/login"
         "?service=https://onlineservice-api.zhihuishu.com/login/gologin")
MAX_IMG = 8


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


def hole_x(bg_bytes, jg_bytes):
    """返回 (hole_left_natural, score)：缺口左边界，在背景图自然像素坐标系里。"""
    import cv2
    import numpy as np
    B = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    J = cv2.imdecode(np.frombuffer(jg_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    a = J[:, :, 3]
    ys, xs = np.where(a > 10)
    if len(xs) == 0:
        return 0, 0.0
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    tpl = J[y0:y1 + 1, x0:x1 + 1, :3]
    band = B[y0:y1 + 1, :, :3]
    res = cv2.matchTemplate(band, tpl, cv2.TM_CCOEFF_NORMED)
    _, s, _, loc = cv2.minMaxLoc(res)
    return loc[0], float(s)


def main():
    u, pw = cred()
    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--window-size=1280,900",
         "--remote-debugging-port=%d" % PORT, "--user-data-dir=/tmp/cdp-cl", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                http("/json/version")
                break
            except Exception:
                pass
        try:
            t = http("/json/new?about:blank", "PUT")
        except Exception:
            t = http("/json/new?about:blank")
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=25,
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

        def geom():
            return json.loads(ev("""JSON.stringify((()=>{const bg=document.querySelector('img.yidun_bg-img'),
                jg=document.querySelector('img.yidun_jigsaw'), sl=document.querySelector('.yidun_slider');
                if(!bg||!jg||!sl) return {ok:false};
                const rb=bg.getBoundingClientRect(), rj=jg.getBoundingClientRect(), rs=sl.getBoundingClientRect();
                return {ok:true,bg:bg.src,jg:jg.src,
                        imgLeft:rb.x, imgW:rb.width, sc:rb.width/bg.naturalWidth,
                        pieceX:rj.x, pieceW:rj.width,
                        sx:rs.x+rs.width/2, sy:rs.y+rs.height/2}})())""") or "{}")

        send("Page.enable")
        send("Runtime.enable")

        for attempt in range(1, MAX_IMG + 1):
            call("Page.navigate", {"url": LOGIN})
            time.sleep(4.5)
            ev("""(()=>{const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(el.__proto__,'value').set;
                s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
                const m=document.querySelector('input[name=mobile]'),p=document.querySelector('input[type=password]');
                if(!m||!p) return 0; set(m,%s); set(p,%s);
                const cb=document.querySelector('input[type=checkbox]'); if(cb&&!cb.checked) cb.click(); return 1})()"""
               % (json.dumps(u), json.dumps(pw)))
            time.sleep(1)
            ev("(()=>{const b=document.querySelector('.btn-block__grandient_login'); if(b)b.click(); return 1})()")
            time.sleep(4.5)
            g = geom()
            if not g.get("ok"):
                print("[%d] 无滑块" % attempt)
                continue
            bg = urllib.request.urlopen(g["bg"], timeout=15).read()
            jg = urllib.request.urlopen(g["jg"], timeout=15).read()
            hx, score = hole_x(bg, jg)
            target = g["imgLeft"] + hx * g["sc"]
            print("[%d] score=%.3f 缺口CSS x=%.1f 拼图当前 x=%.1f" % (attempt, score, target, g["pieceX"]))
            if score < 0.5:
                print("    匹配不可信，换图")
                continue

            x0, y0 = g["sx"], g["sy"]
            hx_pos = g["sx"]
            send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0 - 5, "y": y0 + 2})
            time.sleep(random.uniform(0.1, 0.2))
            send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y0,
                                              "button": "left", "buttons": 1, "clickCount": 1})
            time.sleep(random.uniform(0.15, 0.3))

            # 第一段：粗拖到目标附近
            guess = target - g["pieceX"] - 6           # 经验: 先少拖一点，交给闭环补齐
            steps = 40
            for i in range(1, steps + 1):
                p = i / steps
                ease = 3 * p * p - 2 * p * p * p
                send("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x0 + guess * ease + random.uniform(-0.5, 0.5),
                      "y": y0 + random.uniform(-0.6, 0.6), "button": "left", "buttons": 1})
                time.sleep(random.uniform(0.008, 0.022))
            time.sleep(0.45)

            # 闭环微调
            for it in range(6):
                cur = geom()
                if not cur.get("ok"):
                    break
                err = target - cur["pieceX"]
                print("    闭环%d 拼图x=%.1f err=%.1fpx" % (it + 1, cur["pieceX"], err))
                if abs(err) <= 1.2:
                    break
                hx_pos += err
                send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": hx_pos, "y": y0,
                                                  "button": "left", "buttons": 1})
                time.sleep(random.uniform(0.20, 0.35))
            cur = geom()
            err = target - cur.get("pieceX", target)
            print("    释放前 拼图x=%.1f err=%.1fpx" % (cur.get("pieceX", -1), err))
            time.sleep(random.uniform(0.1, 0.2))
            send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": hx_pos, "y": y0,
                                              "button": "left", "buttons": 0, "clickCount": 1})
            ok = False
            for _ in range(10):
                time.sleep(0.7)
                s = json.loads(ev("""JSON.stringify({m:!!document.querySelector('.yidun_modal'),
                    v:(document.querySelector('input[name=NECaptchaValidate]')||{}).value||''})""") or "{}")
                if (s.get("v") or "").strip():
                    ok = True
                    break
                if not s.get("m"):
                    break
            print("    通过=%s" % ok)
            if ok:
                ev("(()=>{const b=document.querySelector('.btn-block__grandient_login'); if(b)b.click(); return 1})()")
                time.sleep(6)
                print("最终 URL:", (ev("location.href") or "")[:80])
                return 0
        print("未通过")
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
