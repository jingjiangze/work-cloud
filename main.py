import logging
import os
import json
import argparse
import random
import concurrent.futures
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable

from coreApi.MainLogicApi import ApiClient, SubmitUnknownError, CaptchaExhaustedError
from coreApi.AiServiceClient import generate_article
from util.Config import ConfigManager
from util.MessagePush import MessagePusher
from util.HelperFunctions import desensitize_name, is_holiday
from util.FileUploader import upload_img
from util.structured_logging import setup_file_logging
from util.report_validator import (
    validate_report,
    check_duplicate,
    record_report,
    DEFAULT_MIN_LENGTH,
)
from coreApi.auth_checker import ensure_login
from models.execution_history import append_entry
from util.preflight import run_preflight
from models.risk_ledger import (
    record_event,
    set_active_risk_dir,
    EVENT_CAPTCHA_CIRCUIT_BREAK,
    EVENT_DUPLICATE_PREVENTED,
    EVENT_AUTH_FAILURE,
    EVENT_SECOND_INSTANCE_BLOCKED,
    EVENT_SUBMIT_UNKNOWN,
)
from models.task_state import (
    TaskStateStore,
    derive_user_key,
    running_state_for,
    success_state_for,
    STATE_FAILED,
    STATE_LOGIN_SUCCESS,
    STATE_UNKNOWN,
)
from core.account_context import AccountContext
from services.session_manager import default_session_manager

# 日志上下文支持
_log_ctx = threading.local()


class UserTagFormatter(logging.Formatter):
    def format(self, record):
        # 在格式化前添加 userTag 属性
        record.userTag = getattr(_log_ctx, "tag", "-")
        return super().format(record)


# 配置日志
_root_logger = logging.getLogger()
if not _root_logger.handlers:
    # 创建控制台处理器
    handler = logging.StreamHandler()
    formatter = UserTagFormatter(
        fmt="[%(asctime)s] %(name)s %(levelname)s [%(userTag)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    _root_logger.addHandler(handler)
    _root_logger.setLevel(logging.INFO)
    # Stage 8: 结构化 JSON 文件日志（logs/YYYY/MM/DD/app.log），失败不影响主流程
    setup_file_logging()

logger = logging.getLogger(__name__)

USER_DIR = os.path.join(os.path.dirname(__file__), "user")
# Stage 3: 账户级运行数据根目录与注册表路径（显式传递，禁止内部隐式回退）
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "accounts", "index.json")


def _resolve_submit(submit_fn, verify_fn, task_label: str,
                    user_key: str = "unknown"):
    """L3: 提交 → (明确成功/UNKNOWN) → 只读核验 → 最多一次补偿。

    规则：
    - UNKNOWN 不自动转换为成功或失败，用只读查询收敛；
    - 补偿提交最多一次；补偿结果仍为 UNKNOWN 时直接停止（不循环）；
    - 明确成功后的只读核验仅作审计（advisory），不改变成功判定。

    Returns:
        (status, message): status ∈ {"success", "unknown", "fail"}
    """
    try:
        submit_fn()
    except SubmitUnknownError as e:
        logger.error(f"{task_label}提交结果未知，进行只读核验: {e}")
        record_event(user_key, task_label, EVENT_SUBMIT_UNKNOWN,
                     stage="submit", action="只读核验", result=str(e))
        try:
            exists = verify_fn()
        except Exception as ve:  # noqa: BLE001
            return "unknown", f"{task_label}提交结果未知且核验失败: {ve}"
        if exists:
            logger.warning(f"{task_label}服务端已存在记录，判定成功")
            record_event(user_key, task_label, EVENT_SUBMIT_UNKNOWN,
                         stage="verify", action="判定成功", result="服务端已存在记录")
            return "success", f"{task_label}提交结果未知但服务端已存在记录，判定成功"
        logger.warning(f"{task_label}服务端无记录，执行最多一次补偿提交")
        record_event(user_key, task_label, EVENT_SUBMIT_UNKNOWN,
                     stage="verify", action="补偿提交(最多一次)", result="服务端无记录")
        try:
            submit_fn()
        except SubmitUnknownError as e2:
            record_event(user_key, task_label, EVENT_SUBMIT_UNKNOWN,
                         stage="compensate", action="停止(不循环)", result=str(e2))
            return "unknown", f"{task_label}补偿提交结果仍未知，按规则停止: {e2}"
        except Exception as e2:  # noqa: BLE001
            return "fail", f"{task_label}补偿提交失败: {e2}"
        logger.info(f"{task_label}补偿提交成功")
        return "success", f"{task_label}补偿提交成功"
    except Exception as e:  # noqa: BLE001
        return "fail", f"{task_label}提交失败: {e}"
    # 明确成功 → 只读核验（advisory，不改变判定）
    try:
        if not verify_fn():
            logger.warning(f"{task_label}提交响应成功但服务端暂未查到记录，以提交响应为准")
    except Exception as ve:  # noqa: BLE001
        logger.warning(f"{task_label}只读核验失败（不影响成功判定）: {ve}")
    return "success", f"{task_label}提交成功"


def _report_exists_on_server(api_client: ApiClient, report_type: str,
                             current_time: datetime, count: int) -> bool:
    """L3: 只读核验服务端本周期是否已有报告（与去重判定同一口径）。"""
    info = api_client.get_submitted_reports_info(report_type)
    reports = info.get("data") or []
    if not reports:
        return False
    last = reports[0]
    if report_type == "day":
        ct = last.get("createTime")
        if not ct:
            return False
        try:
            return datetime.strptime(
                ct, "%Y-%m-%d %H:%M:%S").date() == current_time.date()
        except ValueError:
            return False
    if report_type == "week":
        return last.get("weeks") == f"第{count}周"
    if report_type == "month":
        return last.get("yearmonth") == current_time.strftime("%Y-%m")
    return False


def perform_clock_in(
    api_client: ApiClient,
    config: ConfigManager,
    state_store: Optional[TaskStateStore] = None,
    user_key: str = "unknown",
) -> Dict[str, Any]:
    """执行打卡操作"""
    task_name = "checkin"
    # Stage 2 幂等保护：本地状态显示当日已成功则直接跳过（服务端检查仍保留兜底）
    if state_store and state_store.is_done(user_key, task_name):
        logger.info("本地状态显示今日打卡已完成，跳过")
        return {
            "status": "skip",
            "message": "今日打卡已完成（本地状态）",
            "task_type": "打卡",
        }
    try:
        current_time = datetime.now()
        current_hour = current_time.hour

        # 确定打卡类型
        if current_hour < 12:
            checkin_type = "START"
            display_type = "上班"
        else:
            checkin_type = "END"
            display_type = "下班"

        # 检查配置：是否跳过节假日/自定义日期
        clock_in_mode = config.get_value("config.clockIn.mode")
        special_clock_in = config.get_value("config.clockIn.specialClockIn")

        should_skip = False
        skip_message = ""

        if clock_in_mode == "holiday" and is_holiday(current_time):
            if not special_clock_in:
                should_skip = True
                skip_message = "今天是休息日，已跳过打卡"
            else:
                checkin_type = "HOLIDAY"
                display_type = "休息/节假日"

        elif clock_in_mode == "custom":
            today_weekday = current_time.weekday() + 1
            custom_days = config.get_value("config.clockIn.customDays") or []
            if today_weekday not in custom_days:
                if not special_clock_in:
                    should_skip = True
                    skip_message = "今天不在设置打卡时间范围内，已跳过打卡"
                else:
                    checkin_type = "HOLIDAY"
                    display_type = "休息/节假日"

        if should_skip:
            if state_store:
                state_store.mark(user_key, task_name, "SKIPPED", skip_message)
            return {
                "status": "skip",
                "message": skip_message,
                "task_type": "打卡",
            }

        if state_store:
            state_store.mark(user_key, task_name, running_state_for(task_name))

        last_checkin_info = api_client.get_checkin_info()

        # 检查是否已经打过卡
        if last_checkin_info and last_checkin_info.get("type") == checkin_type:
            create_time_str = last_checkin_info.get("createTime")
            if create_time_str:
                last_checkin_time = datetime.strptime(
                    create_time_str, "%Y-%m-%d %H:%M:%S"
                )
                if last_checkin_time.date() == current_time.date():
                    logger.info(f"今日 {display_type} 卡已打，无需重复打卡")
                    record_event(user_key, "打卡", EVENT_DUPLICATE_PREVENTED,
                                 stage="pre-submit", action="跳过重复提交",
                                 result=f"今日{display_type}卡已打")
                    return {
                        "status": "skip",
                        "message": f"今日 {display_type} 卡已打，无需重复打卡",
                        "task_type": "打卡",
                    }

        user_name = desensitize_name(config.get_value("userInfo.nikeName"))
        logger.info(f"用户 {user_name} 开始 {display_type} 打卡")

        # 打卡图片和备注
        attachments = upload_img(
            api_client.get_upload_token(),
            config.get_value("userInfo.orgJson.snowFlakeId"),
            config.get_value("userInfo.userId"),
            config.get_value("config.clockIn.imageCount"),
            user_key=user_key,
        )

        description_list = config.get_value("config.clockIn.description")
        description = random.choice(description_list) if description_list else None

        # 设置打卡信息
        checkin_info = {
            "type": checkin_type,
            "lastDetailAddress": last_checkin_info.get("address"),
            "attachments": attachments or None,
            "description": description,
        }

        # L3: 提交 → (明确成功/UNKNOWN) → 只读核验 → 最多一次补偿
        status, submit_message = _resolve_submit(
            lambda: api_client.submit_clock_in(checkin_info),
            lambda: api_client.server_has_checkin(
                checkin_type, current_time.date().isoformat()),
            f"{display_type}打卡",
            user_key=user_key,
        )

        if status == "success":
            logger.info(f"用户 {user_name} {display_type} 打卡成功")
            if state_store:
                state_store.mark(
                    user_key, task_name, success_state_for(task_name),
                    submit_message,
                )
            return {
                "status": "success",
                "message": f"{display_type}打卡成功",
                "task_type": "打卡",
                "details": {
                    "姓名": config.get_value("userInfo.nikeName"),
                    "打卡类型": display_type,
                    "打卡时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "打卡地点": config.get_value("config.clockIn.location.address"),
                },
            }
        if status == "unknown":
            logger.error(f"打卡最终状态未知: {submit_message}")
            if state_store:
                state_store.mark(user_key, task_name, STATE_UNKNOWN, submit_message)
            return {"status": "unknown", "message": submit_message,
                    "task_type": "打卡"}
        logger.error(f"打卡失败: {submit_message}")
        if state_store:
            state_store.mark(user_key, task_name, STATE_FAILED, submit_message)
        return {"status": "fail", "message": submit_message, "task_type": "打卡"}
    except CaptchaExhaustedError as e:
        # L4: 验证码熔断 —— 停止当前任务，阻断本用户后续任务
        logger.error(f"[CAPTCHA_EXHAUSTED] 打卡因验证码熔断失败: {e}")
        record_event(user_key, "打卡", EVENT_CAPTCHA_CIRCUIT_BREAK,
                     stage="submit", action="熔断停止", result=str(e))
        if state_store:
            state_store.mark(user_key, task_name, STATE_FAILED, f"验证码熔断: {e}")
        return {"status": "fail", "message": f"验证码熔断: {str(e)}",
                "task_type": "打卡", "captcha_exhausted": True}
    except SubmitUnknownError as e:
        # L2: 提交结果未知 —— 不判成功也不判失败，等待 L3 只读核验/下次运行核验
        logger.error(f"打卡提交结果未知: {e}")
        record_event(user_key, "打卡", EVENT_SUBMIT_UNKNOWN,
                     stage="submit", action="标记UNKNOWN", result=str(e))
        if state_store:
            state_store.mark(user_key, task_name, STATE_UNKNOWN, str(e))
        return {"status": "unknown",
                "message": f"打卡提交结果未知（待服务端核验）: {str(e)}",
                "task_type": "打卡"}
    except Exception as e:
        logger.error(f"打卡失败: {e}")
        if state_store:
            state_store.mark(user_key, task_name, STATE_FAILED, str(e))
        return {"status": "fail", "message": f"打卡失败: {str(e)}", "task_type": "打卡"}


def _submit_report_common(
    api_client: ApiClient,
    config: ConfigManager,
    report_type: str,
    title_func: Callable[[int], str],
    check_time_func: Callable[[datetime], bool],
    get_submitted_func: Callable[[], Dict[str, Any]],
    paper_num_key: str,
    image_count_key: str,
    task_name: str,
    form_type: int,
    state_store: Optional[TaskStateStore] = None,
    user_key: str = "unknown",
) -> Dict[str, Any]:
    """通用日报/周报/月报提交逻辑"""

    # 映射 report_type 到 config key 与本地状态任务名（Stage 1）
    config_key_map = {"day": "daily", "week": "weekly", "month": "monthly"}
    state_task_map = {
        "day": "daily_report",
        "week": "weekly_report",
        "month": "monthly_report",
    }
    config_key = config_key_map.get(report_type)
    state_task = state_task_map.get(report_type, report_type)

    # Stage 2 幂等保护：本地状态显示本周期已成功提交则直接跳过
    if state_store and state_store.is_done(user_key, state_task):
        logger.info(f"本地状态显示本周期{task_name}已提交，跳过")
        return {
            "status": "skip",
            "message": f"本周期{task_name}已提交（本地状态）",
            "task_type": task_name,
        }

    if not config.get_value(f"config.reportSettings.{config_key}.enabled"):
        logger.info(f"用户未开启{task_name}功能，跳过")
        if state_store:
            state_store.mark(user_key, state_task, "SKIPPED", f"用户未开启{task_name}功能")
        return {
            "status": "skip",
            "message": f"用户未开启{task_name}功能",
            "task_type": task_name,
        }

    current_time = datetime.now()

    # 检查提交时间
    if not check_time_func(current_time):
        logger.info(f"未到{task_name}提交时间")
        if state_store:
            state_store.mark(user_key, state_task, "SKIPPED", f"未到{task_name}提交时间")
        return {
            "status": "skip",
            "message": f"未到{task_name}提交时间",
            "task_type": task_name,
        }

    try:
        if state_store:
            state_store.mark(user_key, state_task, running_state_for(state_task))

        # 检查是否已提交
        submitted_reports_info = get_submitted_func()
        submitted_reports = submitted_reports_info.get("data", [])

        # 检查重复逻辑 (略有不同，由调用者保证 get_submitted_func 返回正确数据)
        # 对于日报：检查日期
        # 对于周报：检查 weeks 字段
        # 对于月报：检查 yearmonth 字段

        count = submitted_reports_info.get("flag", 0) + 1
        title = title_func(count)

        if submitted_reports:
            last_report = submitted_reports[0]
            should_skip = False

            if report_type == "day":
                last_time = datetime.strptime(
                    last_report["createTime"], "%Y-%m-%d %H:%M:%S"
                )
                if last_time.date() == current_time.date():
                    should_skip = True
            elif report_type == "week":
                # 周报 title 类似 "第X周周报"，或者 weeks 字段 "第X周"
                # API 返回的 weeks 字段比较可靠
                current_week_info = api_client.get_weeks_date()[0]
                current_week_str = f"第{count}周"  # 注意这里 count 是基于 flag+1，可能不准确如果重复提交
                # 更稳健的方式：检查 last_report 的 createTime 是否在当前周范围内
                # 但原代码是用 weeks 字符串匹配
                if last_report.get("weeks") == current_week_str:
                    should_skip = True
            elif report_type == "month":
                current_yearmonth = current_time.strftime("%Y-%m")
                if last_report.get("yearmonth") == current_yearmonth:
                    should_skip = True

            if should_skip:
                logger.info(f"本周期已经提交过{task_name}，跳过")
                record_event(user_key, state_task, EVENT_DUPLICATE_PREVENTED,
                             stage="pre-submit", action="跳过重复提交",
                             result=f"本周期已提交过{task_name}")
                if state_store:
                    state_store.mark(user_key, state_task, "SKIPPED", f"本周期已经提交过{task_name}")
                return {
                    "status": "skip",
                    "message": f"本周期已经提交过{task_name}",
                    "task_type": task_name,
                }

        # 生成内容
        job_info = api_client.get_job_info()
        content = generate_article(
            config,
            title,
            job_info,
            config.get_value(paper_num_key),
        )

        # Stage 6: 内容校验 + 重复检测
        min_len = max(DEFAULT_MIN_LENGTH, int(config.get_value(paper_num_key) or 0))
        ok, issues = validate_report(content, min_length=min_len)
        if not ok:
            raise ValueError(f"报告内容校验未通过: {'；'.join(issues)}")
        is_dup, ref_date = check_duplicate(user_key, state_task, content)
        if is_dup:
            logger.warning(f"{title}与 {ref_date} 的内容高度相似，重新生成一次")
            content = generate_article(
                config, title, job_info, config.get_value(paper_num_key))
            ok2, issues2 = validate_report(content, min_length=min_len)
            if not ok2:
                raise ValueError(f"报告内容校验未通过: {'；'.join(issues2)}")
            if check_duplicate(user_key, state_task, content)[0]:
                content = (
                    f"{content}\n报告日期：{current_time.strftime('%Y-%m-%d')}"
                )
                logger.warning("重新生成后仍相似，已附加报告日期区分内容")

        # 上传图片
        attachments = upload_img(
            api_client.get_upload_token(),
            config.get_value("userInfo.orgJson.snowFlakeId"),
            config.get_value("userInfo.userId"),
            config.get_value(image_count_key),
            user_key=user_key,
        )

        report_info = {
            "title": title,
            "content": content,
            "attachments": attachments,
            "reportType": report_type,
            "jobId": job_info.get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": api_client.get_from_info(form_type),
        }

        # 特定类型的额外字段
        extra_details = {}
        if report_type == "week":
            current_week_info = api_client.get_weeks_date()[0]
            report_info["startTime"] = current_week_info.get("startTime")
            report_info["endTime"] = current_week_info.get("endTime")
            report_info["weeks"] = f"第{count}周"
            extra_details = {
                "开始时间": report_info["startTime"],
                "结束时间": report_info["endTime"],
            }
        elif report_type == "month":
            report_info["yearmonth"] = current_time.strftime("%Y-%m")
            extra_details = {"提交月份": report_info["yearmonth"]}

        # L3: 提交 → (明确成功/UNKNOWN) → 只读核验 → 最多一次补偿
        status, submit_message = _resolve_submit(
            lambda: api_client.submit_report(report_info),
            lambda: _report_exists_on_server(
                api_client, report_type, current_time, count),
            title,
            user_key=user_key,
        )

        if status == "success":
            logger.info(f"{title}已提交")
            if state_store:
                state_store.mark(user_key, state_task, success_state_for(state_task), submit_message)
            record_report(user_key, state_task, content)
            return {
                "status": "success",
                "message": f"{title}已提交",
                "task_type": task_name,
                "details": {
                    "标题": title,
                    "提交时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "附件": attachments,
                    **extra_details,
                },
                "report_content": content,
            }
        if status == "unknown":
            logger.error(f"{title}最终状态未知: {submit_message}")
            if state_store:
                state_store.mark(user_key, state_task, STATE_UNKNOWN, submit_message)
            return {"status": "unknown", "message": submit_message,
                    "task_type": task_name}
        logger.error(f"{title}提交失败: {submit_message}")
        if state_store:
            state_store.mark(user_key, state_task, STATE_FAILED, submit_message)
        return {
            "status": "fail",
            "message": submit_message,
            "task_type": task_name,
        }

    except CaptchaExhaustedError as e:
        # L4: 验证码熔断
        logger.error(f"[CAPTCHA_EXHAUSTED] {task_name}因验证码熔断失败: {e}")
        record_event(user_key, state_task, EVENT_CAPTCHA_CIRCUIT_BREAK,
                     stage="submit", action="熔断停止", result=str(e))
        if state_store:
            state_store.mark(user_key, state_task, STATE_FAILED, f"验证码熔断: {e}")
        return {"status": "fail", "message": f"验证码熔断: {str(e)}",
                "task_type": task_name, "captcha_exhausted": True}
    except SubmitUnknownError as e:
        # L2: 提交结果未知 —— 不判成功也不判失败
        logger.error(f"{task_name}提交结果未知: {e}")
        record_event(user_key, state_task, EVENT_SUBMIT_UNKNOWN,
                     stage="submit", action="标记UNKNOWN", result=str(e))
        if state_store:
            state_store.mark(user_key, state_task, STATE_UNKNOWN, str(e))
        return {"status": "unknown",
                "message": f"{task_name}提交结果未知（待服务端核验）: {str(e)}",
                "task_type": task_name}
    except Exception as e:
        logger.error(f"{task_name}提交失败: {e}")
        if state_store:
            state_store.mark(user_key, state_task, STATE_FAILED, str(e))
        return {
            "status": "fail",
            "message": f"{task_name}提交失败: {str(e)}",
            "task_type": task_name,
        }


def submit_daily_report(
    api_client: ApiClient,
    config: ConfigManager,
    state_store: Optional[TaskStateStore] = None,
    user_key: str = "unknown",
) -> Dict[str, Any]:
    """提交日报"""
    return _submit_report_common(
        api_client=api_client,
        config=config,
        state_store=state_store,
        user_key=user_key,
        report_type="day",
        title_func=lambda c: f"第{c}天日报",
        check_time_func=lambda t: t.hour >= 12,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("day"),
        paper_num_key="planInfo.planPaper.dayPaperNum",
        image_count_key="config.reportSettings.daily.imageCount",
        task_name="日报提交",
        form_type=7,
    )


def submit_weekly_report(
    config: ConfigManager,
    api_client: ApiClient,
    state_store: Optional[TaskStateStore] = None,
    user_key: str = "unknown",
) -> Dict[str, Any]:
    """提交周报"""
    submit_day = config.get_value("config.reportSettings.weekly.submitTime")

    def check_time(t: datetime) -> bool:
        # weekday() 返回 0-6 (周一-周日)，配置通常是 1-7
        return (t.weekday() + 1 == submit_day) and (t.hour >= 12)

    return _submit_report_common(
        api_client=api_client,
        config=config,
        state_store=state_store,
        user_key=user_key,
        report_type="week",
        title_func=lambda c: f"第{c}周周报",
        check_time_func=check_time,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("week"),
        paper_num_key="planInfo.planPaper.weekPaperNum",
        image_count_key="config.reportSettings.weekly.imageCount",
        task_name="周报提交",
        form_type=8,
    )


def submit_monthly_report(
    config: ConfigManager,
    api_client: ApiClient,
    state_store: Optional[TaskStateStore] = None,
    user_key: str = "unknown",
) -> Dict[str, Any]:
    """提交月报"""
    submit_day = config.get_value("config.reportSettings.monthly.submitTime")

    def check_time(t: datetime) -> bool:
        # 计算当月最后一天
        next_month = t.replace(day=28) + timedelta(days=4)
        last_day_of_month = (next_month - timedelta(days=next_month.day)).day
        target_day = min(submit_day, last_day_of_month)
        return (t.day == target_day) and (t.hour >= 12)

    return _submit_report_common(
        api_client=api_client,
        config=config,
        state_store=state_store,
        user_key=user_key,
        report_type="month",
        title_func=lambda c: f"第{c}月月报",
        check_time_func=check_time,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("month"),
        paper_num_key="planInfo.planPaper.monthPaperNum",
        image_count_key="config.reportSettings.monthly.imageCount",
        task_name="月报提交",
        form_type=9,
    )


def run(config: ConfigManager,
        context: Optional[AccountContext] = None) -> List[Dict[str, Any]]:
    """执行所有任务（返回结果列表供执行历史记录）

    context: 账户执行上下文（Stage 2 起显式传递，禁止依赖全局变量判账号）；
             None 时保持 legacy 行为（环境变量配置 / 旧调用方兼容）。
    """
    # 设置日志上下文标签（显式身份：context.account_id 优先，绝不取自全局变量）
    try:
        nickname = desensitize_name(config.get_value("userInfo.nikeName")) or "?"
        if context:
            _log_ctx.tag = f"{context.account_id}|{nickname}"
        else:
            file_part = "ENV"
            path_attr = getattr(config, "_path", None)
            if path_attr:
                file_part = os.path.splitext(os.path.basename(str(path_attr)))[0]
            _log_ctx.tag = f"{file_part}|{nickname}"
    except Exception:
        _log_ctx.tag = "-"

    results: List[Dict[str, Any]] = []
    pusher = None
    started_at = datetime.now().strftime("%H:%M:%S")
    start_dt = datetime.now()
    user_key = "unknown"

    try:
        pusher = MessagePusher(config.get_value("config.pushNotifications"))

        # Stage 3: 账户级运行目录隔离——有 context 时状态/历史/风险全部
        # 落入 data/accounts/{account_id}/，主键使用稳定 account_id；
        # 无 context（legacy）路径行为与旧版完全一致
        if context:
            state_store = TaskStateStore(data_dir=context.state_dir)
            user_key = context.user_key
            set_active_risk_dir(context.risk_dir)
        else:
            state_store = TaskStateStore()
            user_key = derive_user_key(config)

        # L5: 启动前只读预检（0 次业务请求 + 至多 1 次 TCP 探测），
        # 关键条件不满足直接 STOP，不带着必败状态发起登录/提交
        report = run_preflight(config, state_store, user_key)
        if report.should_stop:
            if report.benign:
                results.append({
                    "status": "skip",
                    "message": f"预检跳过: {report.summary()}",
                    "task_type": "预检",
                })
            else:
                results.append({
                    "status": "fail",
                    "message": f"预检未通过: {report.summary()}",
                    "task_type": "预检",
                })
            return results

        # Stage 4: 显式绑定账户上下文；创建即登记会话（token 取自本账户
        # 配置），后续重登/验证/失效均只作用于本账户
        api_client = ApiClient(config, context=context)
        if context:
            session_manager = default_session_manager()
            session_manager.register(context.account_id,
                                     config.get_value("userInfo.token") or "")
        api_client.user_key = user_key  # L6: 风险事件台账归属用户
        # Stage 3: 登录检查层——会话预检 + 失败分类（密码/验证码/超时/网络/服务端）
        login_ok, _category, login_message = ensure_login(api_client)
        if context:
            if login_ok:
                session_manager.mark_verified(context.account_id)
            else:
                # 仅失效本账户会话，其他账户不受影响
                session_manager.invalidate(context.account_id)
        if state_store:
            state_store.mark(
                user_key, "login",
                STATE_LOGIN_SUCCESS if login_ok else STATE_FAILED,
                login_message,
            )
        if not login_ok:
            record_event(user_key, "login", EVENT_AUTH_FAILURE,
                         stage="login", action="终止本轮", result=_category or login_message)
            raise RuntimeError(login_message)

        logger.info("获取用户信息成功")

        if config.get_value("userInfo.userType") == "teacher":
            logger.info("用户身份为教师，跳过计划信息检查")
        elif not config.get_value("planInfo.planId"):
            api_client.fetch_internship_plan()
            logger.info("已获取实习计划信息")

        logger.info(
            f"开始执行：{desensitize_name(config.get_value('userInfo.nikeName'))}"
        )

        # L4: 顺序执行；验证码熔断时立即阻断本用户剩余任务（失败保护）
        task_funcs = [
            ("打卡", lambda: perform_clock_in(api_client, config, state_store, user_key)),
            ("日报提交", lambda: submit_daily_report(api_client, config, state_store, user_key)),
            ("周报提交", lambda: submit_weekly_report(config, api_client, state_store, user_key)),
            ("月报提交", lambda: submit_monthly_report(config, api_client, state_store, user_key)),
        ]
        results = []
        for idx, (label, fn) in enumerate(task_funcs):
            result = fn()
            results.append(result)
            if result.get("captcha_exhausted"):
                logger.error(f"[CAPTCHA_EXHAUSTED] {label}触发验证码熔断，"
                             f"跳过本用户剩余 {len(task_funcs) - idx - 1} 个任务")
                for remaining_label, _fn in task_funcs[idx + 1:]:
                    results.append({
                        "status": "skip",
                        "message": "验证码熔断，已跳过",
                        "task_type": remaining_label,
                    })
                    if state_store:
                        state_store.mark(user_key, remaining_label, "SKIPPED",
                                         "验证码熔断，已跳过")
                break

    except Exception as e:
        error_message = f"执行任务时发生严重错误: {str(e)}"
        logger.exception(error_message)  # Stage 8/9: 完整堆栈进文件日志
        results.append(
            {"status": "fail", "message": error_message, "task_type": "系统错误"}
        )
    finally:
        if pusher:
            try:
                pusher.push(results)
            except Exception as e:
                logger.error(f"消息推送失败: {e}")

        logger.info(
            f"执行结束：{desensitize_name(config.get_value('userInfo.nikeName'))}"
        )
        _log_ctx.tag = "-"
        # Stage 10: 每日执行历史台账（Stage 3: 账户级 history/ 目录）
        if context:
            append_entry(user_key, results, started_at,
                         (datetime.now() - start_dt).total_seconds(),
                         history_dir=context.history_dir)
            set_active_risk_dir(None)  # 解绑线程级风险目录路由
        else:
            append_entry(user_key, results, started_at,
                         (datetime.now() - start_dt).total_seconds())
        return results


def _execute_tasks_impl(selected_files: Optional[List[str]] = None):
    """创建并执行任务（调用方需已持有单实例锁）"""
    _log_ctx.tag = "MAIN"
    logger.info("开始执行工学云任务")

    json_files = []
    try:
        if os.path.exists(USER_DIR):
            json_files = [f[:-5] for f in os.listdir(USER_DIR) if f.endswith(".json")]
            logger.info(f"发现 {len(json_files)} 个配置文件")
        else:
            logger.warning(f"用户目录不存在: {USER_DIR}")
    except OSError as e:
        logger.error(f"扫描配置文件目录失败: {e}")

    if selected_files:
        existing_files = set(selected_files) & set(json_files)
        missing_files = set(selected_files) - existing_files
        if missing_files:
            logger.error(f"以下配置文件未找到: {', '.join(missing_files)}")
        json_files = list(existing_files)

    user_configs = []
    user_env = os.getenv("USER")
    if user_env and user_env.strip():
        try:
            user_configs = json.loads(user_env)
            if not isinstance(user_configs, list):
                logger.error("环境变量 USER 必须包含 JSON 数组")
                user_configs = []
            else:
                logger.info(f"从环境变量中获取到 {len(user_configs)} 个配置")
        except json.JSONDecodeError as e:
            logger.error(f"USER 不是有效的JSON格式: {e}")

    if not json_files and not user_configs:
        logger.warning("未找到任何有效配置")
        return

    tasks = []

    for config_data in user_configs:
        try:
            tasks.append(ConfigManager(config=config_data))
        except Exception as e:
            logger.error(f"加载环境变量配置失败: {e}")

    for name in json_files:
        try:
            file_path = os.path.join(USER_DIR, f"{name}.json")
            tasks.append(ConfigManager(path=file_path))
        except Exception as e:
            logger.error(f"加载配置文件 {name} 失败: {e}")

    if not tasks:
        logger.error("没有成功创建任何任务")
        return

    # Stage 2: 每个任务显式构建 AccountContext（自动注册进 registry）；
    # 构建失败不阻断 legacy 路径——context=None 时 run() 行为与旧版一致
    task_contexts: List[Optional[AccountContext]] = []
    for task in tasks:
        try:
            task_contexts.append(AccountContext.from_config(
                task, user_dir=USER_DIR, registry_path=REGISTRY_PATH,
                data_dir=DATA_DIR))
        except Exception as e:
            logger.error(f"构建账户上下文失败（降级为 legacy 模式）: {e}")
            task_contexts.append(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_task = {
            executor.submit(run, task, ctx): task
            for task, ctx in zip(tasks, task_contexts)
        }
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            try:
                future.result()
            except Exception as e:
                logger.exception(f"任务处理过程中发生错误: {e}")

    logger.info("工学云任务执行结束")


def execute_tasks(selected_files: Optional[List[str]] = None):
    """单实例入口：同一台机器同时只允许一个 work-cloud 进程执行（L1）"""
    from util.local_run_lock import LocalRunLock, default_lock_path
    run_lock = LocalRunLock(default_lock_path())
    if not run_lock.acquire():
        logger.error("检测到另一个 work-cloud 进程正在运行，本次运行退出（单实例锁）")
        record_event("process", "全局", EVENT_SECOND_INSTANCE_BLOCKED,
                     stage="startup", action="立即退出",
                     result="另一进程持有运行锁")
        return
    try:
        _execute_tasks_impl(selected_files)
    finally:
        run_lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行工学云任务")
    parser.add_argument(
        "--file",
        type=str,
        nargs="+",
        help="指定要执行的配置文件名（不带路径和后缀），可以一次性指定多个",
    )
    args = parser.parse_args()
    execute_tasks(args.file)
