# -*- coding: utf-8 -*-
"""诊断：CDP 鼠标事件能不能真正拖动易盾滑块。

对比拖动前后滑块的 getBoundingClientRect().x 与截图，判断是
(a) 事件没打到元素（坐标/层级问题），还是 (b) 打了但被判定为机器轨迹。
"""
import base64
import json
import subprocess
import time
import urllib.request

import websocket

PORT = 9232
LOGIN = ("https://passport.zhihuishu.com/login"
         "?service=https://onlineservice-api.zhihuishu.com/login/gologin")


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


def main():
    u, pw = cred()
    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--window-size=1280,900",
         "--remote-debugging-port=%d" % PORT, "--user-data-dir=/tmp/cdp-diag", "about:blank"],
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

        send("Page.enable")
        send("Runtime.enable")
        call("Page.navigate", {"url": LOGIN})
        time.sleep(5)
        ev("""(()=>{const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(el.__proto__,'value').set;
            s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
            set(document.querySelector('input[name=mobile]'),%s);
            set(document.querySelector('input[type=password]'),%s);
            const cb=document.querySelector('input[type=checkbox]'); if(cb&&!cb.checked) cb.click(); return 1})()"""
           % (json.dumps(u), json.dumps(pw)))
        time.sleep(1)
        ev("(()=>{const b=document.querySelector('.btn-block__grandient_login'); if(b)b.click(); return 1})()")
        time.sleep(5)

        print("=== 控件清单 ===")
        print(ev("""JSON.stringify(Array.from(document.querySelectorAll('[class*=yidun]')).map(e=>{
            const r=e.getBoundingClientRect();
            return [e.tagName, (e.className||'').toString().slice(0,34), Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
        }).slice(0,14))"""))

        before2 = ev("""JSON.stringify((()=>{const s=document.querySelector('.yidun_slider');
            const j=document.querySelector('img.yidun_jigsaw');
            return {slider:Math.round(s.getBoundingClientRect().x), piece:Math.round(j.getBoundingClientRect().x)}})())""")
        print("拖动前 滑块/拼图:", before2)
        before = ev("""JSON.stringify((()=>{const s=document.querySelector('.yidun_slider');
            const r=s?s.getBoundingClientRect():null; return r?{x:r.x,y:r.y,w:r.width,h:r.height}:null})())""")
        print("拖动前滑块:", before)
        b = json.loads(before or "null")
        if not b:
            print("找不到 .yidun_slider")
            return 1
        # 视口信息
        print("视口:", ev("JSON.stringify([window.innerWidth, window.innerHeight, window.devicePixelRatio, document.documentElement.clientWidth])"))

        x0, y0 = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y0})
        time.sleep(0.2)
        send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y0,
                                          "button": "left", "buttons": 1, "clickCount": 1})
        DRAG=float(__import__("os").environ.get("DRAG","100"))
        for i in range(1, 21):
            send("Input.dispatchMouseEvent", {"type": "mouseMoved",
                                              "x": x0 + DRAG * i / 20, "y": y0,
                                              "button": "left", "buttons": 1})
            time.sleep(0.02)
        time.sleep(0.8)  # 等拼图跟随动画稳定
        mid_state = ev("""JSON.stringify((()=>{const s=document.querySelector('.yidun_slider');
            const j=document.querySelector('img.yidun_jigsaw');
            return {slider:Math.round(s.getBoundingClientRect().x), piece:Math.round(j.getBoundingClientRect().x)}})())""")
        print("拖动中(未释放) 滑块/拼图:", mid_state)
        print("拖动前 滑块/拼图:", before2 if 'before2' in dir() else "")
        send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x0 + DRAG, "y": y0,
                                          "button": "left", "buttons": 0, "clickCount": 1})
        time.sleep(1.5)
        after = ev("""JSON.stringify((()=>{const s=document.querySelector('.yidun_slider');
            const r=s?s.getBoundingClientRect():null; return r?{x:Math.round(r.x)}:null})())""")
        print("释放后滑块:", after)
        tip = ev("((document.querySelector('.yidun_tips')||{}).innerText||'').trim()")
        print("提示:", tip)
        shot = call("Page.captureScreenshot", {"format": "png"}, tm=30).get("data")
        if shot:
            open("/tmp/diag_after.png", "wb").write(base64.b64decode(shot))
            print("截图已存 /tmp/diag_after.png")
        return 0
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
