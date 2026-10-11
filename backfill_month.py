# -*- coding: utf-8 -*-
"""仅回填缺失月报（month 接口在批量环境下偶发返回 flag>0 但 data 为空，
故单独做：读到可信数据才提交，且天然幂等 + 服务端周期唯一性兜底）。

只处理确实缺月报的账户（按 probe 已知：仅 108691167 缺 2026-09）。
用法：python backfill_month.py
"""
import os
import sys
import time
import random
import traceback
from datetime import date, datetime, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
import main as M


def plan_window(cfg):
    plan = cfg.get_value("planInfo") or {}
    s = datetime.strptime(str(plan.get("startTime"))[:10], "%Y-%m-%d").date()
    e = datetime.strptime(str(plan.get("endTime"))[:10], "%Y-%m-%d").date()
    return s, e


def expected_months(start, end, today):
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if date(y, m, 1) <= today:
            out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def month_label(ym: str, plan_start: date) -> str:
    y, m = int(ym.split("-")[0]), int(ym.split("-")[1])
    n = (y * 12 + m) - (plan_start.year * 12 + plan_start.month) + 1
    return f"第{n}月"


def read_months_reliable(client, max_attempts=60, gap=6):
    """反复读取月报列表，直到拿到 data 非空且 flag 与 data 数量一致的可信结果。
    返回 (data, flag) 或 在穷尽后返回 ([], 最后flag)。"""
    last_flag = 0
    for attempt in range(max_attempts):
        try:
            if attempt % 3 == 0:
                client.login()
            info = client.get_submitted_reports_info("month")
            data = info.get("data") or []
            flag = info.get("flag")
            if flag is None:
                flag = info.get("totalCount") or info.get("total") or len(data)
            last_flag = flag
            if flag and flag > 0 and not data:
                print(f"  month read try {attempt}: flag={flag} 但 data 空，重试")
                time.sleep(gap + random.uniform(0, 4))
                continue
            # 抽查 yearmonth 是否可读
            yms = [r.get("yearmonth") for r in data if r.get("yearmonth")]
            if flag and flag > 0 and not yms:
                print(f"  month read try {attempt}: flag={flag} 但无 yearmonth 字段({len(data)}条)，重试")
                time.sleep(gap + random.uniform(0, 4))
                continue
            print(f"  month read try {attempt}: OK flag={flag} data_len={len(data)} yms={yms}")
            return data, flag
        except Exception as e:  # noqa: BLE001
            print(f"  month read try {attempt}: 异常 {type(e).__name__}: {str(e)[:120]}")
            time.sleep(gap + random.uniform(0, 4))
    return [], last_flag


def main():
    accounts = ["me", "108687438", "108691167"]
    today = date.today()
    for stem in accounts:
        cfg = ConfigManager(f"user/{stem}.json")
        client = ApiClient(cfg)
        client.login()
        client.fetch_internship_plan()
        pstart, pend = plan_window(cfg)
        exp_mo = expected_months(pstart, pend, today)
        print(f"ACCT|{stem}|PLAN {pstart}~{pend} exp_months={exp_mo}")
        data, flag = read_months_reliable(client)
        covered = {r.get("yearmonth") for r in data if r.get("yearmonth")}
        miss = [m for m in exp_mo if m not in covered]
        print(f"  flag={flag} covered_months={sorted(covered)} miss_months={miss}")
        if not miss:
            print(f"  {stem}: 月报无缺口，跳过")
            continue
        if flag and flag > 0 and not covered:
            print(f"  WARN|{stem}: 月列表读不到可信数据(flag={flag})，放弃以免误填")
            continue
        for ym in miss:
            try:
                client.login()
                client.fetch_internship_plan()
                job_info = client.get_job_info()
                if ym in covered:
                    print(f"SKIP|{stem}|month|{ym} already covered")
                    continue
                label = month_label(ym, pstart)
                title = f"{label}月报"
                paper_num = cfg.get_value("planInfo.planPaper.monthPaperNum")
                image_count = cfg.get_value("config.reportSettings.monthly.imageCount") or 0
                report_info = {
                    "title": title, "content": "", "attachments": "",
                    "reportType": "month", "jobId": job_info.get("jobId"),
                    "reportTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "formFieldDtoList": client.get_from_info(9),
                    "yearmonth": ym,
                }
                content = M.generate_article(cfg, title, job_info, paper_num)
                ok, issues = M.validate_report(content, min_length=max(M.DEFAULT_MIN_LENGTH, int(paper_num or 0)))
                if not ok:
                    raise ValueError(f"内容校验未通过: {'；'.join(issues)}")
                report_info["content"] = content
                report_info["attachments"] = M.upload_img(
                    client.get_upload_token(),
                    cfg.get_value("userInfo.orgJson.snowFlakeId"),
                    cfg.get_value("userInfo.userId"),
                    image_count, user_key=stem,
                )
                status, msg = M._resolve_submit(
                    lambda: client.submit_report(report_info),
                    lambda: ym in covered,
                    title, user_key=stem,
                )
                print(f"RESULT|{stem}|month|{ym}|status={status}|msg={msg}")
            except Exception as e:
                print(f"ERR|{stem}|month|{ym}|{type(e).__name__}: {str(e)[:300]}")
                traceback.print_exc()
            time.sleep(3)
    print("=== DONE ===")


if __name__ == "__main__":
    main()
