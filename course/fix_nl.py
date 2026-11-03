# -*- coding: utf-8 -*-
"""修 zhs_daily_v2.py 的未定义变量 NL（NameError，导致整条日链每次都崩）。"""
import pathlib
import shutil
import sys
import time

P = pathlib.Path("/opt/zhexam/zhs_daily_v2.py")
OLD = "NL.join("
NEW = '"\\n".join('


def main():
    raw = P.read_bytes().decode("utf-8")
    n = raw.count(OLD)
    print("occurrences:", n)
    if n == 0:
        print("nothing to do")
        return 0
    bak = str(P) + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(str(P), bak)
    print("backup ->", bak)
    P.write_bytes(raw.replace(OLD, NEW).encode("utf-8"))
    print("patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
