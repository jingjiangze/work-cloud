# -*- coding: utf-8 -*-
"""把 courses.json 的知到课程列表换成账号里真实可用的 hike 课程（带备份）。

背景：知到 API 对该账号整族 401（getZhidaoList/queryCourse/videolist 全 401），
而 hike 的三门课 getHikeContext 全部正常。fuckCourse 按 ID 是否含字母分流：
纯数字 ⇒ fuckHikeCourse，所以换成数字 ID 后同一条流水线直接可用。
"""
import json
import shutil
import sys
import time

P = "/opt/course-automation/var/state/courses.json"

HIKE = {
    "11497022": "大学生职业发展与就业指导2",
    "11344911": "药学服务实务",
    "10936157": "大学生职业生涯与发展规划",
}


def main():
    raw = open(P, encoding="utf-8").read()
    data = json.loads(raw)
    bak = P + ".bak-%s" % time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(P, bak)
    print("backup ->", bak)

    changed = False
    for acc in data.get("zhidao", []):
        if acc.get("id") != "zh-8da374":
            continue
        old = list(acc.get("course_list") or [])
        if all(i in HIKE for i in old) and len(old) == len(HIKE):
            print("already hike ids, skip")
            return 0
        acc["disabled_course_list"] = old
        acc["disabled_course_names"] = acc.get("course_names") or {}
        acc["course_list"] = list(HIKE.keys())
        acc["course_names"] = dict(HIKE)
        changed = True
        print("course_list: %s -> %s" % (old, acc["course_list"]))

    if changed:
        with open(P, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
