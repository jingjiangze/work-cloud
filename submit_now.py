"""立即提交周报/月报，不受"星期几"限制。

用法:
    python submit_now.py week     # 提交周报
    python submit_now.py month    # 提交月报
"""
import sys

from util.Config import ConfigManager
from coreApi.MainLogicApi import ApiClient
import main as M

REPORT_CONF = {
    "week": dict(
        report_type="week",
        title_func=lambda c: f"第{c}周周报",
        paper_num_key="planInfo.planPaper.weekPaperNum",
        image_count_key="config.reportSettings.weekly.imageCount",
        task_name="周报提交",
        form_type=8,
    ),
    "month": dict(
        report_type="month",
        title_func=lambda c: f"第{c}月月报",
        paper_num_key="planInfo.planPaper.monthPaperNum",
        image_count_key="config.reportSettings.monthly.imageCount",
        task_name="月报提交",
        form_type=9,
    ),
}


def main(kind: str) -> int:
    conf = REPORT_CONF[kind]
    cfg = ConfigManager("user/me.json")
    client = ApiClient(cfg)
    client.login()
    client.fetch_internship_plan()

    result = M._submit_report_common(
        api_client=client,
        config=cfg,
        report_type=conf["report_type"],
        title_func=conf["title_func"],
        check_time_func=lambda t: True,          # 立即提交，不看星期
        get_submitted_func=lambda: client.get_submitted_reports_info(
            conf["report_type"]),
        paper_num_key=conf["paper_num_key"],
        image_count_key=conf["image_count_key"],
        task_name=conf["task_name"],
        form_type=conf["form_type"],
    )
    print("\n===== 提交结果 =====")
    print("状态  :", result.get("status"))
    print("说明  :", result.get("message"))
    details = result.get("details") or {}
    for k, v in details.items():
        if k == "报告内容":
            v = str(v)[:80] + "..."
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "week"
    if kind not in REPORT_CONF:
        print("参数只能是 week 或 month")
        sys.exit(1)
    sys.exit(main(kind))
