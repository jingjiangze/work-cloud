# -*- coding: utf-8 -*-
"""把真实客户端的请求头喂进来，验证 /gateway/t/** 的 401 会不会消失。

用法（在 ECS 上）：
    /opt/course-automation/venv/bin/python /opt/course-automation/src/fuckCourse/probe_with_headers.py \
        'cookie: aliyun_waf=xxx; SESSION=xxx; CASTGC=xxx' \
        'token: eyJhbGciOi...' \
        'x-token: abc'

  每个参数都是 "名字: 值" 形式；脚本会：
    1) 原样带上这些头请求 queryCourse（不加密、纯 form），打印状态码与响应体
    2) 再带上同一批头 + 官方加密载荷请求一次（走 zhidaoQuery）
    3) 两次都不再 401 ⇒ 说明头对上了，可以据此改 fucker.py

注意：脚本**不保存**这些头，也不打印头的值（只打印头的名字），避免凭据落到日志里。
"""
import json
import os
import sys

ZHS = "/opt/course-automation/src/fuckCourse/zhs"
FDIR = "/opt/course-automation/var/state/fuckCourse"
sys.path.insert(0, ZHS)
os.environ.setdefault("FUCKCOURSE_CONFIG", FDIR + "/config.json")

ACC = os.environ.get("ZHS_ACC", "zh-8da374")
RAC = os.environ.get("ZHS_RAC") or "4e5e5f5142594859454a5859584c5c425e"
URL = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/queryCourse"


def parse(pairs):
    hdrs = {}
    for p in pairs:
        if ":" not in p:
            print("跳过无法解析的参数（需要 '名字: 值'）")
            continue
        k, v = p.split(":", 1)
        hdrs[k.strip()] = v.strip()
    return hdrs


def main():
    hdrs = parse(sys.argv[1:])
    if not hdrs:
        print("用法: probe_with_headers.py 'header: value' ['header2: value2' ...]")
        return 2
    print("将测试的头部: %s" % sorted(hdrs))

    import requests
    from fucker import Fucker

    jar = json.loads(open(FDIR + "/cookies-%s.json" % ACC, encoding="utf-8").read()).get("zhs") or []
    f = Fucker(tree_view=False, progressbar_view=False)
    f.cookies = jar
    f._sessionReady()
    f.session.headers.update({"Origin": "https://studyh5.zhihuishu.com",
                              "Referer": "https://studyh5.zhihuishu.com/"})
    f.session.headers.update(hdrs)

    r = f.session.post(URL, data={"recruitAndCourseId": RAC}, timeout=15)
    print("[plain form] %s %s" % (r.status_code, r.text[:200].replace("\n", " ")))
    if r.status_code != 401:
        print("*** 401 已消失（普通 form 请求即可通过）***")

    try:
        f.zhidaoQuery(URL, {"recruitAndCourseId": RAC})
        print("[encrypted ] OK —— 401 已消失，可以据此改 fucker.py")
    except Exception as exc:
        print("[encrypted ] FAIL %s" % str(exc)[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
