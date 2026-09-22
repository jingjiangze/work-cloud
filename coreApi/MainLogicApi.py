import json
import logging
import time
import uuid
import random
import threading
from typing import Dict, Any, List, Optional

import requests

from util.Config import ConfigManager
from util.CryptoUtils import create_sign, aes_encrypt, aes_decrypt
from util.CaptchaUtils import recognize_blockPuzzle_captcha, recognize_clickWord_captcha
from util.HelperFunctions import get_current_month_info
from util.request_helper import is_retryable_exception

# 尝试导入主模块的日志上下文，失败则创建本地版本
try:
    from main import _log_ctx
except ImportError:
    _log_ctx = threading.local()

logger = logging.getLogger(__name__)


class CaptchaExhaustedError(Exception):
    """验证码处理超限（L4 熔断）。

    验证码是异常事件而非普通重试场景：获取/识别失败达到上限后立即停止
    当前任务并向上传播，由调用方决定是否阻断后续任务（失败保护，
    而非自动突破）。
    """


class SubmitUnknownError(Exception):
    """提交类请求结果未知（L2）。

    触发条件：提交类 POST 发出后未收到服务端响应（超时/连接重置/传输中断），
    无法确定服务端是否已受理 —— 此时**不得**判定为 FAILED，也**不得**盲目重试。
    由调用方进行只读核验（L3）后收敛为 SUCCESS / FAILED / UNKNOWN。
    """


class ApiClient:
    """
    ApiClient类用于与远程服务器进行交互，包括用户登录、获取实习计划、获取打卡信息、提交打卡等功能。
    """
    BASE_URL = "https://api.moguding.net:9000/"
    DEFAULT_HEADERS = {
        "user-agent": "Dart/2.17 (dart:io)",
        "content-type": "application/json; charset=utf-8",
        "accept-encoding": "gzip",
        "host": "api.moguding.net:9000",
    }

    def __init__(self, config: ConfigManager):
        """
        初始化ApiClient实例。

        Args:
            config (ConfigManager): 用于管理配置的实例。
        """
        self.config = config
        self.max_retries = 5  # 控制重新尝试的次数
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    def _post_request(
        self,
        url: str,
        headers: Dict[str, str],
        data: Dict[str, Any],
        submit: bool = False,
        allow_302: bool = False,
    ) -> Dict[str, Any]:
        """
        发送POST请求，并处理请求过程中可能发生的错误。
        包括自动重试机制和Token失效处理。
        """
        full_url = f"{self.BASE_URL}{url}"
        
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    full_url,
                    headers=headers,
                    json=data,
                    timeout=10
                )
                response.raise_for_status()
                rsp = response.json()
                
                code = rsp.get("code")
                msg = rsp.get("msg", "未知错误")

                # 特殊情况处理
                # L4 修正：allow_302=True 时把 302（行为验证码）正常返回给
                # 提交方处理 —— 否则 submit_clock_in 的验证码分支是死代码
                if code == 200:
                    if msg == "302" and not allow_302:
                        raise ValueError("打卡失败，触发行为验证码")
                    return rsp
                
                if code == 6111:
                    return rsp

                # Token失效处理
                if "token失效" in msg:
                    if attempt < self.max_retries - 1:
                        wait_time = 1 * (2 ** attempt)
                        logger.warning(f"Token失效，正在重新登录... (等待 {wait_time}s)")
                        time.sleep(wait_time)
                        
                        self.login()
                        # 更新headers中的authorization
                        headers["authorization"] = self.config.get_value("userInfo.token")
                        continue
                    else:
                        raise ValueError(msg)
                
                raise ValueError(msg)

            except (requests.RequestException, ValueError) as e:
                # raise_for_status 抛出的 HTTPError 兜底补挂 response，
                # 保证状态码判断准确（正常 requests 会自动挂载）
                if isinstance(e, requests.exceptions.HTTPError) \
                        and getattr(e, "response", None) is None:
                    try:
                        e.response = response
                    except Exception:  # noqa: BLE001
                        pass

                # L2: 提交类请求结果无法确定 → UNKNOWN，不重试不判失败
                #  - 未收到响应（超时/重置/中断）→ UNKNOWN
                #  - 5xx（网关/后端是否受理未知）→ UNKNOWN
                #  - 4xx：服务端明确拒绝 → 走下方业务失败路径
                if submit and not isinstance(e, ValueError):
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if not isinstance(e, requests.exceptions.HTTPError):
                        raise SubmitUnknownError(
                            f"提交后未收到服务端响应: {e}") from e
                    if code is not None and code >= 500:
                        raise SubmitUnknownError(
                            f"提交后服务端返回 {code}，受理状态未知: {e}") from e

                # Stage 4: 精确重试分类（替代中文启发式，RISK-C04）
                # - 业务错误(ValueError)不重试
                # - 仅对"确定未到达服务端"的错误重试，缓解重复提交（RISK-B06）
                if isinstance(e, ValueError):
                    raise

                if not is_retryable_exception(e):
                    raise ValueError(str(e))

                if attempt >= self.max_retries - 1:
                    raise ValueError(str(e))

                wait_time = 1 * (2 ** attempt)
                logger.warning(f"请求失败: {e}，重试 {attempt + 1}/{self.max_retries}，等待 {wait_time:.2f} 秒")
                time.sleep(wait_time)
        
        raise ValueError("请求失败，超过最大重试次数")

    def pass_blockPuzzle_captcha(self, max_attempts: int = 5) -> str:
        """通过行为验证码（blockPuzzle）"""
        for attempt in range(max_attempts):
            try:
                captcha_url = "session/captcha/v1/get"
                request_data = {
                    "clientUid": str(uuid.uuid4()).replace("-", ""),
                    "captchaType": "blockPuzzle",
                }
                captcha_info = self._post_request(captcha_url, self.DEFAULT_HEADERS, request_data)
                
                slider_data = recognize_blockPuzzle_captcha(
                    captcha_info["data"]["jigsawImageBase64"],
                    captcha_info["data"]["originalImageBase64"],
                )
                
                check_slider_url = "session/captcha/v1/check"
                check_slider_data = {
                    "pointJson": aes_encrypt(slider_data, captcha_info["data"]["secretKey"], "b64"),
                    "token": captcha_info["data"]["token"],
                    "captchaType": "blockPuzzle",
                }
                
                check_result = self._post_request(check_slider_url, self.DEFAULT_HEADERS, check_slider_data)
                
                if check_result.get("code") != 6111:
                    return aes_encrypt(
                        captcha_info["data"]["token"] + "---" + slider_data,
                        captcha_info["data"]["secretKey"],
                        "b64",
                    )
            except Exception as e:
                logger.warning(f"滑块验证尝试 {attempt + 1}/{max_attempts} 失败: {e}")
                time.sleep(random.uniform(1, 3))
                
        raise CaptchaExhaustedError(
            f"滑块验证码连续 {max_attempts} 次未通过，熔断停止")

    def solve_click_word_captcha(self, max_retries: int = 5) -> str:
        """通过点选验证码（clickWord）"""
        for retry in range(max_retries):
            try:
                captcha_endpoint = "/attendence/clock/v1/get"
                captcha_request_payload = {
                    "clientUid": str(uuid.uuid4()).replace("-", ""),
                    "captchaType": "clickWord",
                }

                captcha_response = self._post_request(
                    captcha_endpoint,
                    self._get_authenticated_headers(),
                    captcha_request_payload,
                )

                captcha_solution = recognize_clickWord_captcha(
                    captcha_response["data"]["originalImageBase64"],
                    captcha_response["data"]["wordList"],
                )

                verification_endpoint = "/attendence/clock/v1/check"
                verification_payload = {
                    "pointJson": aes_encrypt(captcha_solution, captcha_response["data"]["secretKey"], "b64"),
                    "token": captcha_response["data"]["token"],
                    "captchaType": "clickWord",
                }

                verification_response = self._post_request(
                    verification_endpoint,
                    self._get_authenticated_headers(),
                    verification_payload,
                )

                if verification_response.get("code") != 6111:
                    return aes_encrypt(
                        captcha_response["data"]["token"] + "---" + captcha_solution,
                        captcha_response["data"]["secretKey"],
                        "b64",
                    )
                
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                logger.warning(f"点选验证尝试 {retry + 1}/{max_retries} 失败: {e}")
                time.sleep(random.uniform(1, 3))

        raise CaptchaExhaustedError(
            f"点选验证码连续 {max_retries} 次未通过，熔断停止")

    def login(self) -> None:
        """执行用户登录操作"""
        url = "session/user/v6/login"
        data = {
            "phone": aes_encrypt(self.config.get_value("config.user.phone")),
            "password": aes_encrypt(self.config.get_value("config.user.password")),
            "captcha": self.pass_blockPuzzle_captcha(),
            "loginType": "android",
            "uuid": str(uuid.uuid4()).replace("-", ""),
            "device": "android",
            "version": "5.16.0",
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        rsp = self._post_request(url, self.DEFAULT_HEADERS, data)
        user_info = json.loads(aes_decrypt(rsp.get("data", "")))
        self.config.update_config(user_info, "userInfo")

    def fetch_internship_plan(self) -> None:
        """获取当前用户的实习计划"""
        url = "practice/plan/v3/getPlanByStu"
        data = {
            "pageSize": 999999,
            "t": aes_encrypt(str(int(time.time() * 1000)))
        }
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            self.config.get_value("userInfo.roleKey"),
        ])
        rsp = self._post_request(url, headers, data)
        plan_info = rsp.get("data", [{}])[0]
        self.config.update_config(plan_info, "planInfo")

    def get_job_info(self) -> Dict[str, Any]:
        """获取用户的工作ID"""
        url = "practice/job/v4/infoByStu"
        data = {
            "planId": self.config.get_value("planInfo.planId"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        headers = self._get_authenticated_headers()
        # 这里重试已经在 _post_request 中处理了，但保留这里的特殊处理（如果需要的话）
        # 原代码 retry_count=3 是针对 _post_request 的递归，现在 _post_request 内部处理重试
        rsp = self._post_request(url, headers, data)
        data = rsp.get("data", {})
        return {} if data is None else data

    def get_submitted_reports_info(self, report_type: str) -> Dict[str, Any]:
        """获取已经提交的日报、周报或月报的数量"""
        url = "practice/paper/v2/listByStu"
        data = {
            "currPage": 1,
            "pageSize": 10,
            "reportType": report_type,
            "planId": self.config.get_value("planInfo.planId"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            self.config.get_value("userInfo.roleKey"),
            report_type,
        ])
        rsp = self._post_request(url, headers, data)
        return rsp

    def submit_report(self, report_info: Dict[str, Any]) -> None:
        """提交报告"""
        url = "practice/paper/v6/save"
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            report_info.get("reportType"),
            self.config.get_value("planInfo.planId"),
            report_info.get("title"),
        ])
        
        # 使用 dict.fromkeys 初始化所有字段为 None
        keys = [
            "address", "applyId", "applyName", "attachmentList", "commentNum",
            "commentContent", "content", "createBy", "createTime", "depName",
            "reject", "endTime", "headImg", "yearmonth", "imageList", "isFine",
            "latitude", "gpmsSchoolYear", "longitude", "planId", "planName",
            "reportId", "reportType", "reportTime", "isOnTime", "schoolId",
            "startTime", "state", "studentId", "studentNumber", "supportNum",
            "title", "url", "username", "weeks", "videoUrl", "videoTitle",
            "attachments", "companyName", "jobName", "jobId", "score",
            "tpJobId", "starNum", "confirmDays", "isApply", "compStarNum",
            "compScore", "compComment", "compState", "apply", "levelEntity",
            "formFieldDtoList", "fieldEntityList", "feedback", "handleWay",
            "isWarning", "warningType", "t"
        ]
        data = dict.fromkeys(keys, None)
        
        # 更新必要字段
        data.update({
            "content": report_info.get("content"),
            "planId": self.config.get_value("planInfo.planId"),
            "reportType": report_info.get("reportType"),
            "title": report_info.get("title"),
            "jobId": report_info.get("jobId", ""),
            "attachments": report_info.get("attachments", ""),
            "formFieldDtoList": report_info.get("formFieldDtoList", []),
            "fieldEntityList": [],
            "isWarning": 0,
            "t": aes_encrypt(str(int(time.time() * 1000))),
        })
        
        # 更新可选字段（如果存在）
        for key in ["endTime", "startTime", "yearmonth", "reportTime", "weeks"]:
            if key in report_info:
                data[key] = report_info[key]

        self._post_request(url, headers, data, submit=True)

    def get_weeks_date(self) -> List[Dict[str, Any]]:
        """获取本周周报周期信息"""
        url = "practice/paper/v3/getWeeks1"
        data = {"t": aes_encrypt(str(int(time.time() * 1000)))}
        headers = self._get_authenticated_headers()
        rsp = self._post_request(url, headers, data)
        return rsp.get("data", [])

    def get_from_info(self, formType: int) -> List[Dict[str, Any]]:
        """获取子表单（问卷），并设置值"""
        url = "practice/paper/v2/info"
        data = {
            "formType": formType,
            "t": aes_encrypt(str(int(time.time() * 1000)))
        }
        headers = self._get_authenticated_headers()
        rsp = self._post_request(url, headers, data).get("data", {})
        formFieldDtoList = rsp.get("formFieldDtoList", [])
        
        if not formFieldDtoList:
            return formFieldDtoList
            
        logger.info("检测到问卷，已自动填写")
        for item in formFieldDtoList:
            item["value"] = "b" # 默认选择B
            
        return formFieldDtoList

    def get_checkin_info(self) -> Dict[str, Any]:
        """获取用户的打卡信息"""
        url = "attendence/clock/v2/listSynchro"
        if self.config.get_value("userInfo.userType") == "teacher":
            url = "attendence/clock/teacher/v1/listSynchro"
            
        headers = self._get_authenticated_headers()
        data = {
            **get_current_month_info(),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        rsp = self._post_request(url, headers, data)
        return rsp.get("data", [{}])[0] if rsp.get("data") else {}

    def submit_clock_in(self, checkin_info: Dict[str, Any]) -> None:
        """提交打卡信息"""
        url = "attendence/clock/teacher/v2/save"
        sign_data = None
        planId = self.config.get_value("planInfo.planId")

        if self.config.get_value("userInfo.userType") != "teacher":
            url = "attendence/clock/v5/save"
            sign_data = [
                self.config.get_value("config.device"),
                checkin_info.get("type"),
                planId,
                self.config.get_value("userInfo.userId"),
                self.config.get_value("config.clockIn.location.address"),
            ]

        logger.info(f'打卡类型：{checkin_info.get("type")}')
        
        # 初始化所有可能的字段为None
        keys = [
            "distance", "content", "lastAddress", "lastDetailAddress", "attendanceId", 
            "country", "createBy", "createTime", "description", "device", "images", 
            "isDeleted", "isReplace", "modifiedBy", "modifiedTime", "schoolId", 
            "state", "teacherId", "teacherNumber", "type", "stuId", "planId", 
            "attendanceType", "username", "attachments", "userId", "isSYN", 
            "studentId", "applyState", "studentNumber", "memberNumber", "headImg", 
            "attendenceTime", "depName", "majorName", "className", "logDtoList", 
            "isBeyondFence", "practiceAddress", "tpJobId", "t"
        ]
        data = dict.fromkeys(keys, None)

        data.update({
            "country": "中国",
            "createTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "description": checkin_info.get("description", None),
            "device": self.config.get_value("config.device"),
            "state": "NORMAL",
            "type": checkin_info.get("type"),
            "planId": planId,
            "attachments": checkin_info.get("attachments", None),
            "userId": self.config.get_value("userInfo.userId"),
            "lastDetailAddress": checkin_info.get("lastDetailAddress"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        })

        data.update(self.config.get_value("config.clockIn.location"))

        headers = self._get_authenticated_headers(sign_data)

        response = self._post_request(url, headers, data, submit=True, allow_302=True)
        if response.get("msg") == "302":
            logger.info("检测到行为验证码，正在通过···")
            data["captcha"] = self.solve_click_word_captcha()
            # RISK-C02 修复：二次提交必须校验结果，避免"假成功"
            retry_response = self._post_request(url, headers, data, submit=True, allow_302=True)
            if retry_response.get("msg") == "302":
                # L4: 二次提交仍要求验证码 → 处理超限，熔断
                raise CaptchaExhaustedError("验证码校验未通过（二次提交仍要求验证码），打卡未提交")

    def get_upload_token(self) -> str:
        """获取上传文件的认证令牌"""
        url = "session/upload/v1/token"
        headers = self._get_authenticated_headers()
        data = {"t": aes_encrypt(str(int(time.time() * 1000)))}
        rsp = self._post_request(url, headers, data)
        return rsp.get("data", "")

    def server_has_checkin(self, checkin_type: str, today_str: str) -> bool:
        """只读核验：服务端当日是否已存在指定类型的打卡记录（L3）。

        不产生任何写操作；查询失败由调用方处理。
        """
        info = self.get_checkin_info()
        if not info or info.get("type") != checkin_type:
            return False
        create = info.get("createTime")
        if not create:
            return False
        try:
            from datetime import datetime as _dt
            return _dt.strptime(create, "%Y-%m-%d %H:%M:%S").date().isoformat() == today_str
        except ValueError:
            return False

    def _get_authenticated_headers(
        self,
        sign_data: Optional[List[Optional[str]]] = None
    ) -> Dict[str, str]:
        """生成带有认证信息的请求头"""
        headers = self.DEFAULT_HEADERS.copy()
        headers.update({
            "authorization": self.config.get_value("userInfo.token"),
            "userid": self.config.get_value("userInfo.userId"),
            "rolekey": self.config.get_value("userInfo.roleKey"),
        })
        
        if sign_data:
            headers["sign"] = create_sign(*sign_data)
        return headers
