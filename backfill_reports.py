# -*- coding: utf-8 -*-
"""回填缺失的周报/月报（多账户改造 Stage: backfill）。

原 submit 通道只能提交"当前"周期（get_weeks_date()[0] + reportTime=now），
无法补齐历史周期。本脚本复用已有的 api_client.submit_report()（其 report_info
本身已接受 startTime/endTime/weeks/yearmonth 等周期字段），为"缺失的历史周期"
构造并提交。

安全设计：
- 周期按 周一~周日 严格对齐（服务端校验 startTime 必须是周一）。
- 幂等：用【预先抓取】的已提交周期集合（字符串元组，与期望集同型）判定缺失；
  提交路径不再二次拉取接口（避免误判为缺失而重复提交）。
- 服务端本身也有周期唯一性校验（"此时间段已经写过周记"），作为最后兜底。
- 列表读取带重试：若服务端宣称有记录(flag>0)却返回空 data，视为偶发空响应
  并重试；持续失败时保留最后一次的 flag 以便保守跳过（宁可漏填不可误填）。
- --pilot：只处理首个账户的第一个【真正缺失】周期，先验证服务端是否接受回填。
- --dry-run：只打印缺口，不提交。

用法：
  python backfill_reports.py --dry-run
  python backfill_reports.py --pilot
  python backfill_reports.py
"""
import os
import sys
import time
import argparse
import traceback
from datetime import date, datetime, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
import main as M


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def first_monday_on_or_after(d: date) -> date:
    m = monday_of(d)
    return m if m >= d else m + timedelta(days=7)


def plan_window(cfg):
    plan = cfg.get_value("planInfo") or {}
    s = datetime.strptime(str(plan.get("startTime"))[:10], "%Y-%m-%d").date()
    e = datetime.strptime(str(plan.get("endTime"))[:10], "%Y-%m-%d").date()
    return s, e


def expected_weeks(start, end, today):
    """返回字符串元组列表 ('YYYY-MM-DD','YYYY-MM-DD')，周一~周日对齐。"""
    out = []
    cur = first_monday_on_or_after(start)
    while cur <= end:
        a = cur                            # 必为周一
        b = cur + timedelta(days=6)         # 周日
        if b > end:
            b = end
        if b <= today:
            out.append((a.isoformat(), b.isoformat()))
        cur = cur + timedelta(days=7)
    return out


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


def week_label(start_str, anchor: date) -> str:
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    n = ((start - anchor).days // 7) + 1
    return f"第{n}周"


def month_label(ym: str, plan_start: date) -> str:
    y, m = int(ym.split("-")[0]), int(ym.split("-")[1])
    n = (y * 12 + m) - (plan_start.year * 12 + plan_start.month) + 1
    return f"第{n}月"


def covered(reports):
    weeks, months = set(), set()
    for r in reports:
        st = str(r.get("startTime") or "")[:10]
        en = str(r.get("endTime") or "")[:10]
        if st and en:
            weeks.add((st, en))
        if r.get("yearmonth"):
            months.add(r.get("yearmonth"))
    return weeks, months


def get_list(client, rtype, retries=20, gap=6):
    """带重试地拉取已提交列表，返回 (data, flag, reliable)。
    - 若 flag>0 但 data 为空（偶发空响应），刷新登录后重试。
    - 全部尝试失败/持续空响应时，返回 ([], 最后已知flag, False)，
      由调用方据 flag 保守跳过以免误填。
    """
    last = None
    last_flag = 0
    for attempt in range(retries):
        try:
            info = client.get_submitted_reports_info(rtype)
            data = info.get("data") or []
            flag = info.get("flag")
            if flag is None:
                flag = info.get("totalCount") or info.get("total") or len(data)
            last_flag = flag
            if flag and flag > 0 and not data:
                last = Exception(f"flag={flag} 但 data 为空（偶发空响应）")
                # 偶发空响应：刷新登录并延长退避后重试
                try:
                    client.login()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(gap)
                continue
            return data, flag, True
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(gap)
    print(f"  WARN|{rtype}|列表持续空/失败: {last}")
    return [], last_flag, False


def backfill_one(stem, rtype, target_week, target_month, cov_wk, cov_mo, anchor, plan_start, dry):
    cfg = ConfigManager(f"user/{stem}.json")
    client = ApiClient(cfg)
    client.login()
    client.fetch_internship_plan()
    job_info = client.get_job_info()

    if rtype == "week":
        key = target_week
        a, b = target_week
        label = week_label(a, anchor)
        title = f"{label}周报"
        paper_num = cfg.get_value("planInfo.planPaper.weekPaperNum")
        image_count = cfg.get_value("config.reportSettings.weekly.imageCount") or 0
        form_type = 8
        report_info = {
            "title": title, "content": "", "attachments": "",
            "reportType": "week", "jobId": job_info.get("jobId"),
            "reportTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": client.get_from_info(form_type),
            "startTime": f"{a} 00:00:00", "endTime": f"{b} 23:59:59",
            "weeks": label,
        }
    else:
        key = target_month
        label = month_label(target_month, plan_start)
        title = f"{label}月报"
        paper_num = cfg.get_value("planInfo.planPaper.monthPaperNum")
        image_count = cfg.get_value("config.reportSettings.monthly.imageCount") or 0
        form_type = 9
        report_info = {
            "title": title, "content": "", "attachments": "",
            "reportType": "month", "jobId": job_info.get("jobId"),
            "reportTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": client.get_from_info(form_type),
            "yearmonth": target_month,
        }

    # 幂等：用预先抓取的覆盖集合判定（同型字符串元组）
    if (rtype == "week" and key in cov_wk) or (rtype == "month" and key in cov_mo):
        print(f"SKIP|{stem}|{rtype}|period {key} already covered")
        return "skip"
    if dry:
        print(f"DRY|{stem}|{rtype}|period {key} title={title} (不提交)")
        return "dry"

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
        lambda: (rtype == "week" and key in cov_wk) or (rtype == "month" and key in cov_mo),
        title, user_key=stem,
    )
    print(f"RESULT|{stem}|{rtype}|period={key}|status={status}|msg={msg}")
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    accounts = ["me", "108687438", "108691167"]
    today = date.today()
    pilot_done = False
    for stem in accounts:
        cfg = ConfigManager(f"user/{stem}.json")
        client = ApiClient(cfg)
        client.login()
        client.fetch_internship_plan()
        pstart, pend = plan_window(cfg)
        plan_start = pstart
        # 周序号锚点：优先用已提交周里最早的周一，否则用首个不早于 planStart 的周一
        anchor = first_monday_on_or_after(pstart)
        print(f"ACCT|{stem}|PLAN {pstart}~{pend} anchorMon={anchor}")

        wk_reports, wk_flag, wk_ok = get_list(client, "week")
        time.sleep(5)
        mo_reports, mo_flag, mo_ok = get_list(client, "month")
        cov_wk, cov_mo = covered(wk_reports)
        if cov_wk:
            earliest = min(datetime.strptime(s, "%Y-%m-%d").date() for s, _ in cov_wk)
            anchor = earliest
        exp_wk = expected_weeks(pstart, pend, today)
        exp_mo = expected_months(pstart, pend, today)
        miss_wk = [w for w in exp_wk if w not in cov_wk]
        miss_mo = [m for m in exp_mo if m not in cov_mo]
        print(f"  wk_flag={wk_flag} reliable={wk_ok} cov_weeks({len(cov_wk)})={sorted(cov_wk)}")
        print(f"  mo_flag={mo_flag} reliable={mo_ok} cov_months({len(cov_mo)})={sorted(cov_mo)}")
        print(f"  miss_weeks={miss_wk}")
        print(f"  miss_months={miss_mo}")
        # 覆盖列表不可信（flag 声明有记录但读不到）→ 保守跳过该类型以免误填
        if wk_flag and wk_flag > 0 and not cov_wk:
            print(f"  WARN|{stem}|周列表不可信(flag={wk_flag}但data空)，跳过周回填")
            miss_wk = []
        if mo_flag and mo_flag > 0 and not cov_mo:
            print(f"  WARN|{stem}|月列表不可信(flag={mo_flag}但data空)，跳过月回填")
            miss_mo = []
        if not cov_wk and not cov_mo and not wk_flag and not mo_flag:
            print(f"  WARN|{stem}|覆盖列表完全为空且无计数，跳过以免误填")
            continue

        for w in miss_wk:
            if args.pilot and pilot_done:
                print(f"  PILOT_STOP|week|{w}")
                continue
            try:
                backfill_one(stem, "week", w, None, cov_wk, cov_mo, anchor, plan_start, args.dry_run)
            except Exception as e:
                print(f"ERR|{stem}|week|{w}|{type(e).__name__}: {str(e)[:300]}")
                traceback.print_exc()
            pilot_done = True
            time.sleep(3)
        for m in miss_mo:
            if args.pilot and pilot_done:
                print(f"  PILOT_STOP|month|{m}")
                continue
            try:
                backfill_one(stem, "month", None, m, cov_wk, cov_mo, anchor, plan_start, args.dry_run)
            except Exception as e:
                print(f"ERR|{stem}|month|{m}|{type(e).__name__}: {str(e)[:300]}")
                traceback.print_exc()
            pilot_done = True
            time.sleep(3)
    print("=== DONE ===")


if __name__ == "__main__":
    main()
