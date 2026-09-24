# -*- coding: utf-8 -*-
"""work-cloud 本地看板（只读）。

- 纯标准库实现（http.server），零第三方依赖，低占用；
- 只读为主：读取 data/ 下的状态/台账/风险文件；Stage 12 起提供**受控
  写操作**（账户启用/停用、任务策略、立即运行）——必须携带与会话绑定
  的 CSRF 令牌，且每个动作落审计台账（data/audit/）；
- 绑定 127.0.0.1，公网暴露一律经 Cloudflare Tunnel；
- 访问控制：登录页（密码表单）→ HttpOnly 会话 Cookie；
  密码存 dashboard/password.txt（可直接编辑更换，服务端只存 SHA-256）；
  登录失败指数退避，/health 免认证供探针。
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(ROOT)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)  # 供账户注册表/执行器导入项目模块
DATA_DIR = os.path.join(PROJECT, "data")
STATIC_DIR = os.path.join(ROOT, "static")

# Stage 12: 管理动作经账户注册表写（注册表路径为项目内绝对路径，与调度器一致）
from models import account_registry  # noqa: E402
PASSWORD_FILE = os.path.join(ROOT, "password.txt")
COOKIE_NAME = "wk_dashboard"
SESSION_TTL = 12 * 3600

PORT = int(os.environ.get("WK_DASHBOARD_PORT", "8792"))

# ---------- 登录密码 ----------

def _load_password_hash() -> str:
    """读 password.txt（明文，可随时编辑更换），内存中只保留 SHA-256。"""
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            pw = f.read().strip()
            if pw:
                return hashlib.sha256(pw.encode("utf-8")).hexdigest()
    pw = secrets.token_urlsafe(9)  # 首次启动自动生成，写入明文文件供查看
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(pw)
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


PASSWORD_HASH = _load_password_hash()

# 登录失败退避（防爆破：连续失败按次数线性加长校验耗时）
_fail_lock = threading.Lock()
_fail_count = 0

# 会话：token -> {"exp": epoch, "csrf": str}（CSRF 与会话一一绑定）
_sessions: dict = {}
_sessions_lock = threading.Lock()

# Stage 12: 账户管理动作审计（operator 全部为看板登录者 "operator"）
AUDIT_DIR = os.path.join(DATA_DIR, "audit")
_audit_lock = threading.Lock()

# Stage 12: 看板内触发的执行任务（防重复点击）
_run_in_progress: set = set()
_run_progress_lock = threading.Lock()


# ---------- Stage 12: 受控管理动作 ----------

def _append_audit(action: str, account_id: str, detail: dict = None) -> dict:
    """管理动作审计：data/audit/{date}.json 追加列表（best-effort，不抛异常）。"""
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": str(action),
        "account_id": str(account_id or ""),
        "detail": detail or {},
    }
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR,
                            datetime.now().strftime("%Y-%m-%d") + ".json")
        with _audit_lock:
            data = _read_json(path)
            if not isinstance(data, list):
                data = []
            data.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return entry


def load_audit(day: str, limit: int = 100) -> list:
    """读某日审计（date=YYYY-MM-DD，严格格式校验）。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day or "")):
        return []
    data = _read_json(os.path.join(AUDIT_DIR, f"{day}.json"))
    if not isinstance(data, list):
        return []
    return data[-limit:]


def _csrf_valid(session_token: str, header_token: str) -> bool:
    """CSRF 令牌校验：必须存在且与会话一一绑定（恒定时间比较）。"""
    expected = _session_csrf(session_token)
    if not expected or not header_token:
        return False
    return hmac.compare_digest(expected, header_token)


def _spawn_run(account) -> dict:
    """拉起独立进程执行该账户任务（python main.py --file <stem>）。

    与计划任务共用单实例运行锁（LocalRunLock）：若全局执行中，子进程
    会立即退出并记风险事件——不会产生并发双重登录。
    """
    file_stem = os.path.splitext(account.config_file)[0]
    key = f"{account.account_id}:{file_stem}"
    with _run_progress_lock:
        if key in _run_in_progress:
            return {"ok": False, "message": "该账户已有一次手动运行进行中，请稍候"}
        _run_in_progress.add(key)
    try:
        python = sys.executable or "python"
        proc = subprocess.Popen(
            [python, "main.py", "--file", file_stem],
            cwd=PROJECT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": True, "pid": proc.pid,
                "message": f"已拉起执行进程（pid={proc.pid}，账户 {file_stem}）"}
    except OSError as exc:
        return {"ok": False, "message": f"拉起执行进程失败: {exc}"}
    finally:
        with _run_progress_lock:
            _run_in_progress.discard(key)


def perform_account_action(account_id: str, action: str,
                           payload: dict = None,
                           registry_path: str = None,
                           user_dir: str = None) -> tuple:
    """受控账户动作分发。返回 (response_dict, http_code)。

    动作：enable / disable / set_task_policy / run。
    registry_path / user_dir 仅测试注入用；生产用注册表默认路径。
    """
    payload = payload if isinstance(payload, dict) else {}
    if not _ACCOUNT_ID_RE.match(str(account_id or "")):
        return {"error": "invalid account id"}, 400

    kwargs = {}
    if registry_path is not None:
        kwargs["registry_path"] = registry_path
    if user_dir is not None:
        kwargs["user_dir"] = user_dir

    if action in ("enable", "disable"):
        fn = (account_registry.enable_account if action == "enable"
              else account_registry.disable_account)
        account = fn(account_id, **kwargs)
        if account is None:
            return {"error": "account not found"}, 404
        _append_audit(action, account_id, {"enabled": account.enabled})
        return {"ok": True, "account_id": account_id,
                "enabled": account.enabled}, 200

    if action == "set_task_policy":
        account = account_registry.set_task_policy(
            account_id, payload.get("task_policy"), **kwargs)
        if account is None:
            return {"error": "account not found"}, 404
        _append_audit(action, account_id,
                      {"task_policy": account.task_policy or {}})
        return {"ok": True, "account_id": account_id,
                "task_policy": account.task_policy or {}}, 200

    if action == "run":
        account = account_registry.get_account(account_id, **kwargs)
        if account is None:
            return {"error": "account not found"}, 404
        result = _spawn_run(account)
        _append_audit(action, account_id, result)
        return ({"ok": True, **result}, 200) if result.get("ok") \
            else ({"ok": False, "message": result.get("message", "")}, 409)

    return {"error": f"unknown action: {action}"}, 400


def _default_account_config(phone: str, password: str) -> dict:
    """新账号默认配置（登录验证通过后落盘 user/{user_key}.json）。

    打卡开启（位置信息待用户补填后才会真正打卡成功）；报告默认关闭
    （AI apikey 未配置时预检会拦截整轮执行）。
    """
    return {"config": {
        "user": {"phone": phone, "password": password},
        "clockIn": {
            "enabled": True, "mode": "daily",
            "location": {"address": "", "latitude": "", "longitude": "",
                         "province": "", "city": "", "area": ""},
            "imageCount": 0,
            "description": ["今日实习工作正常开展", "按时到岗，完成日常工作",
                            "完成今日岗位任务", "按计划开展实习工作"],
            "specialClockIn": False, "customDays": [1, 2, 3, 4, 5],
        },
        "reportSettings": {
            "daily": {"enabled": False, "imageCount": 0},
            "weekly": {"enabled": False, "imageCount": 0, "submitTime": 5},
            "monthly": {"enabled": False, "imageCount": 0, "submitTime": 28},
        },
        "ai": {"model": "gpt-4o-mini", "apikey": "",
               "apiUrl": "https://api.openai.com/"},
        "pushNotifications": [],
        "device": {"brand": "TA J20", "systemVersion": "17",
                   "Platform": "Android", "isPhysical": True},
    }}


def perform_add_account(phone: str, password: str,
                        display_name: str = "") -> tuple:
    """网页添加工学云账号：真实登录验证 → 写配置文件 → 注册。

    返回 (response_dict, http_code)。登录验证直接复用项目主流程
    （ApiClient.login，含滑块验证码 OCR），失败即拒绝入库。
    """
    phone = str(phone or "").strip()
    password = str(password or "")
    if not re.match(r"^1\d{10}$", phone):
        return {"error": "手机号格式不正确"}, 400
    if not password:
        return {"error": "密码不能为空"}, 400

    # 查重：同手机号已注册则拒绝（注册表 config_file 对应 user/*.json）
    for acc in account_registry.list_accounts():
        if os.path.splitext(acc.config_file)[0] == phone or \
                acc.display_name == (display_name or phone):
            return {"error": "该账号已存在", "account_id": acc.account_id}, 409

    from util.Config import ConfigManager
    from models.task_state import derive_user_key

    cfg = _default_account_config(phone, password)
    cm = ConfigManager(config=cfg)
    try:
        from coreApi.MainLogicApi import ApiClient
        api = ApiClient(cm)
        api.login()  # 真实登录（AES + 滑块验证码 OCR），失败抛异常
        try:
            api.fetch_internship_plan()
        except Exception:
            pass  # 计划获取失败不阻断入库
    except Exception as exc:
        _append_audit("add_account_rejected", phone,
                      {"reason": str(exc)[:200]})
        return {"error": f"登录验证失败: {str(exc)[:160]}"}, 401

    user_key = derive_user_key(cm)
    user_dir = os.path.join(PROJECT, "user")
    os.makedirs(user_dir, exist_ok=True)
    config_file = f"{user_key}.json"
    with open(os.path.join(user_dir, config_file), "w",
              encoding="utf-8") as f:
        json.dump(cm._config, f, ensure_ascii=False, indent=2)

    account = account_registry.get_or_register_by_config(
        config_file, display_name=display_name or phone)
    if account is None:
        return {"error": "注册失败（配置文件未被扫描）"}, 500
    if display_name:
        account_registry.update_account(account.account_id,
                                        display_name=display_name)
    _append_audit("add_account", account.account_id,
                  {"phone": phone[:3] + "****" + phone[-4:],
                   "config_file": config_file})
    return {"ok": True, "account_id": account.account_id,
            "display_name": display_name or phone,
            "message": "账号已添加（打卡已开启，位置信息请在该账号配置中补填）"}, 200


def _new_session() -> str:
    token = secrets.token_urlsafe(24)
    csrf = secrets.token_urlsafe(24)
    with _sessions_lock:
        now = datetime.now().timestamp()
        expired = [t for t, v in _sessions.items()
                   if (v["exp"] if isinstance(v, dict) else v) < now]
        for t in expired:
            _sessions.pop(t, None)
        _sessions[token] = {"exp": now + SESSION_TTL, "csrf": csrf}
    return token


def _session_csrf(token: str) -> str:
    if not token:
        return ""
    with _sessions_lock:
        v = _sessions.get(token)
        if isinstance(v, dict) and v.get("exp", 0) > datetime.now().timestamp():
            return v.get("csrf", "")
        _sessions.pop(token, None)
    return ""


def _valid_session(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        v = _sessions.get(token)
        exp = v.get("exp") if isinstance(v, dict) else v
        if exp and exp > datetime.now().timestamp():
            return True
        _sessions.pop(token, None)
    return False


def _parse_cookie(header: str) -> str:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return v
    return ""


# ---------- 只读数据读取 ----------

def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _safe_join(base: str, name: str) -> str:
    """白名单式拼路径：拒绝任何 ..、分隔符与非常见字符。"""
    name = os.path.basename(str(name))
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return ""
    return os.path.join(base, name)


def _load_day_state(date: str) -> list:
    """当日各用户任务状态：data/{date}_{user}.json"""
    users = []
    if not os.path.isdir(DATA_DIR):
        return users
    prefix = f"{date}_"
    for name in sorted(os.listdir(DATA_DIR)):
        if name.startswith(prefix) and name.endswith(".json"):
            payload = _read_json(os.path.join(DATA_DIR, name))
            if isinstance(payload, dict):
                users.append(payload)
    return users


def _load_history(date: str):
    return _read_json(_safe_join(os.path.join(DATA_DIR, "history"),
                                 f"{date}.json"))


def _load_risk(date: str):
    return _read_json(_safe_join(os.path.join(DATA_DIR, "risk"),
                                 f"{date}.json"))


def _load_statistics():
    return _read_json(os.path.join(DATA_DIR, "statistics.json"))


def _list_recent_dates(limit: int = 14) -> list:
    """从执行历史目录取最近 N 个日期（倒序）。"""
    hist_dir = os.path.join(DATA_DIR, "history")
    dates = []
    if os.path.isdir(hist_dir):
        for name in os.listdir(hist_dir):
            if name.endswith(".json"):
                dates.append(name[:-5])
    return sorted(dates, reverse=True)[:limit]


def build_overview() -> dict:
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    history = _load_history(date) or {"date": date, "entries": []}
    return {
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": date,
        "state": _load_day_state(date),
        "history": history,
        "risk": _load_risk(date) or {"date": date, "events": []},
        "statistics": _load_statistics(),
        "recent_dates": _list_recent_dates(),
    }


# ---------- 多账户数据（Stage 11，只读） ----------

ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
_ACCOUNT_ID_RE = re.compile(r"^acct_[A-Za-z0-9]{6,16}$")

# 状态文件里的各种键（中文标签/英文键并存）→ 看板统一标签
_KEY2LABEL = {
    "login": "登录", "checkin": "打卡", "打卡": "打卡",
    "daily_report": "日报", "日报提交": "日报", "daily": "日报",
    "weekly_report": "周报", "周报提交": "周报",
    "monthly_report": "月报", "月报提交": "月报",
    "账户锁": "账户锁",
}
_LABEL_ORDER = ["登录", "打卡", "日报", "周报", "月报", "账户锁"]


def _account_base(account_id: str) -> str:
    """账户数据目录（严格校验 ID 格式，防目录穿越）。"""
    if not _ACCOUNT_ID_RE.match(str(account_id or "")):
        return ""
    return os.path.join(ACCOUNTS_DIR, account_id)


def _registry_accounts() -> list:
    reg = _read_json(os.path.join(ACCOUNTS_DIR, "index.json")) or {}
    return [a for a in reg.get("accounts", []) if isinstance(a, dict)]


def _canonical_tasks(state_payload: dict) -> list:
    """状态快照 → 统一标签任务列表（顺序稳定，后写覆盖先写）。"""
    tasks = (state_payload or {}).get("tasks") or {}
    merged = {}
    for key, info in tasks.items():
        label = _KEY2LABEL.get(key)
        if not label or not isinstance(info, dict):
            continue
        merged[label] = {"state": info.get("state", ""),
                         "message": info.get("message", "")}
    ordered = [(l, merged[l]) for l in _LABEL_ORDER if l in merged]
    # 非标准标签（未来扩展）追加在后
    for label, info in merged.items():
        if label not in _LABEL_ORDER:
            ordered.append((label, info))
    return [{"label": l, **info} for l, info in ordered]


def _account_ledger_runs(account_id: str, day: str) -> list:
    data = _read_json(os.path.join(ACCOUNTS_DIR, account_id,
                                   "ledger", f"{day}.json"))
    return (data or {}).get("runs", []) if isinstance(data, dict) else []


def _account_risk_events(account_id: str, day: str) -> list:
    data = _read_json(os.path.join(ACCOUNTS_DIR, account_id,
                                   "risk", f"{day}.json"))
    return (data or {}).get("events", []) if isinstance(data, dict) else []


def _account_reports_meta(account_id: str, day: str) -> list:
    base = os.path.join(ACCOUNTS_DIR, account_id, "reports", day)
    records = []
    if not os.path.isdir(base):
        return records
    for rtype in sorted(os.listdir(base)):
        rdir = os.path.join(base, rtype)
        if not os.path.isdir(rdir):
            continue
        for name in sorted(os.listdir(rdir)):
            if name.endswith(".json"):
                payload = _read_json(os.path.join(rdir, name))
                if isinstance(payload, dict):
                    records.append(payload)
    return records


def _overall_of_run(run: dict) -> str:
    return normalize_overall(run.get("status", ""))


def normalize_overall(status: str) -> str:
    s = str(status or "").lower()
    if s in ("success",):
        return "success"
    if s in ("failed", "fail"):
        return "failed"
    if s in ("skipped", "skip"):
        return "skipped"
    if s in ("unknown",):
        return "unknown"
    return s or "-"


def build_accounts_overview() -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    accounts_out = []
    counts = {"total": 0, "enabled": 0, "success": 0, "failed": 0,
              "unknown": 0, "skipped": 0}
    last_run_time = None
    for acc in _registry_accounts():
        counts["total"] += 1
        enabled = bool(acc.get("enabled"))
        if enabled:
            counts["enabled"] += 1
        aid = acc.get("account_id", "")
        state_payload = _read_json(os.path.join(
            ACCOUNTS_DIR, aid, "state", f"{today}_{aid}.json"))
        runs = _account_ledger_runs(aid, today)
        last_run = runs[-1] if runs else None
        if last_run:
            overall = normalize_overall(last_run.get("status", ""))
            counts[overall] = counts.get(overall, 0) + 1
            started = last_run.get("started_at", "")
            if started and (last_run_time is None or started > last_run_time):
                last_run_time = started
        accounts_out.append({
            "account_id": aid,
            "display_name": acc.get("display_name", aid),
            "enabled": enabled,
            "tasks": _canonical_tasks(state_payload),
            "last_run": ({
                "run_id": last_run.get("run_id", ""),
                "started_at": last_run.get("started_at", ""),
                "duration_sec": last_run.get("duration_sec"),
                "status": normalize_overall(last_run.get("status", "")),
            } if last_run else None),
        })
    return {
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": today,
        "counts": counts,
        "last_run_time": last_run_time,
        "accounts": accounts_out,
    }


def build_account_detail(account_id: str):
    if not _account_base(account_id):
        return None
    acc = next((a for a in _registry_accounts()
                if a.get("account_id") == account_id), None)
    if acc is None:
        return None

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(7)]

    state_payload = _read_json(os.path.join(
        ACCOUNTS_DIR, account_id, "state", f"{today}_{account_id}.json"))

    daily_series = []
    recent_errors = []
    risk_events = []
    reports = []
    for day in days:
        runs = _account_ledger_runs(account_id, day)
        by_status = {"success": 0, "failed": 0, "unknown": 0, "skipped": 0}
        for run in runs:
            by_status[normalize_overall(run.get("status", ""))] = \
                by_status.get(normalize_overall(run.get("status", "")), 0) + 1
            for task in run.get("tasks", []):
                if str(task.get("status", "")).lower() in ("fail", "failed"):
                    recent_errors.append({
                        "date": day, "run_id": run.get("run_id", ""),
                        "task_type": task.get("task_type", ""),
                        "message": task.get("message", ""),
                    })
        daily_series.append({"date": day, "runs": len(runs), **by_status})
        for ev in _account_risk_events(account_id, day):
            ev["_date"] = day
            risk_events.append(ev)
        reports.extend(_account_reports_meta(account_id, day))

    return {
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "profile": {
            "account_id": account_id,
            "display_name": acc.get("display_name", account_id),
            "enabled": bool(acc.get("enabled")),
            "config_file": acc.get("config_file", ""),
            "task_policy": acc.get("task_policy"),
            "schedule_profile": acc.get("schedule_profile"),
        },
        "today": {
            "date": today,
            "tasks": _canonical_tasks(state_payload),
            "ledger_runs": [
                {"run_id": r.get("run_id", ""),
                 "started_at": r.get("started_at", ""),
                 "duration_sec": r.get("duration_sec"),
                 "status": normalize_overall(r.get("status", "")),
                 "tasks": [
                     {"task_type": t.get("task_type", ""),
                      "status": t.get("status", ""),
                      "message": t.get("message", ""),
                      "verification": t.get("verification")}
                     for t in r.get("tasks", [])
                 ]}
                for r in _account_ledger_runs(account_id, today)
            ],
        },
        "daily_series": daily_series,
        "recent_errors": recent_errors[-10:],
        "risk_events": risk_events[-30:],
        "reports": reports[-20:],
    }


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "WkDash/1.0"

    def log_message(self, fmt, *args):  # 静默：写文件日志太重，控制台无窗口
        pass

    # ---- helpers ----
    def _send(self, code: int, body: bytes, ctype: str,
              extra_headers: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _authed(self) -> bool:
        return _valid_session(_parse_cookie(self.headers.get("Cookie", "")))

    # ---- routes ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._json({"ok": True})
            return

        if path == "/login":
            # 已登录直接进看板；未登录显示登录页（?error=1 显示错误提示）
            if self._authed():
                self._send(302, b"", "text/plain", {"Location": "/"})
                return
            try:
                with open(os.path.join(STATIC_DIR, "login.html"), "rb") as f:
                    body = f.read()
            except OSError:
                self._send(500, b"login page missing", "text/plain")
                return
            if parse_qs(parsed.query).get("error", [""])[0]:
                err_html = '<div class="error">密码错误，请重试</div>'.encode("utf-8")
                body = body.replace(b"<!--ERR-->", err_html)
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/logout":
            token = _parse_cookie(self.headers.get("Cookie", ""))
            with _sessions_lock:
                _sessions.pop(token, None)
            self._send(302, b"", "text/plain",
                       {"Location": "/login",
                        "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0"})
            return

        if not self._authed():
            self._send(302, b"", "text/plain", {"Location": "/login"})
            return

        if path == "/" or path == "/index.html" or path.startswith("/account/"):
            # /account/{id} 也走同一单页，前端按 pathname 切换视图
            try:
                with open(os.path.join(STATIC_DIR, "index.html"),
                          "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"dashboard html missing", "text/plain")
            return

        if path == "/api/overview":
            self._json(build_overview())
            return

        if path == "/api/accounts":
            self._json(build_accounts_overview())
            return

        m = re.match(r"^/api/accounts/(acct_[A-Za-z0-9]{6,16})$", path)
        if m:
            detail = build_account_detail(m.group(1))
            if detail is None:
                self._json({"error": "account not found"}, 404)
            else:
                self._json(detail)
            return

        if path == "/api/csrf":
            token = _parse_cookie(self.headers.get("Cookie", ""))
            csrf = _session_csrf(token)
            if not csrf:
                self._json({"error": "no session"}, 401)
            else:
                self._json({"csrf": csrf})
            return

        if path == "/api/audit":
            day = parse_qs(parsed.query).get("date",
                                             [datetime.now().strftime("%Y-%m-%d")])[0]
            self._json({"date": day, "audit": load_audit(day)})
            return

        if path == "/api/history":
            date = parse_qs(parsed.query).get("date",
                                              [datetime.now().strftime("%Y-%m-%d")])[0]
            self._json({"date": date,
                        "history": _load_history(date),
                        "risk": _load_risk(date)})
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._do_login(parsed)
            return

        # Stage 12: 管理动作——登录 + CSRF 双重校验（CSRF 与会话绑定）
        m = re.match(r"^/api/accounts/(acct_[A-Za-z0-9]{6,16})/action$",
                     parsed.path)
        if m:
            cookie_token = _parse_cookie(self.headers.get("Cookie", ""))
            if not _valid_session(cookie_token):
                self._json({"error": "not authenticated"}, 401)
                return
            if not _csrf_valid(cookie_token,
                               self.headers.get("X-CSRF-Token", "")):
                _append_audit("csrf_rejected", m.group(1), {
                    "path": parsed.path})
                self._json({"error": "csrf token missing or invalid"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(min(length, 65536)).decode("utf-8")
                payload = json.loads(body) if body.strip() else {}
            except (ValueError, OSError):
                self._json({"error": "invalid json body"}, 400)
                return
            action = ""
            if isinstance(payload, dict):
                action = str(payload.get("action", ""))
            resp, code = perform_account_action(m.group(1), action,
                                                payload if isinstance(payload, dict) else {})
            self._json(resp, code)
            return

        # Stage 14: 网页添加工学云账号（真实登录验证，可能耗时 10-40 秒）
        if parsed.path == "/api/accounts/add":
            cookie_token = _parse_cookie(self.headers.get("Cookie", ""))
            if not _valid_session(cookie_token):
                self._json({"error": "not authenticated"}, 401)
                return
            if not _csrf_valid(cookie_token,
                               self.headers.get("X-CSRF-Token", "")):
                _append_audit("csrf_rejected", "add_account",
                              {"path": parsed.path})
                self._json({"error": "csrf token missing or invalid"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(min(length, 65536)).decode("utf-8")
                payload = json.loads(body) if body.strip() else {}
            except (ValueError, OSError):
                self._json({"error": "invalid json body"}, 400)
                return
            if not isinstance(payload, dict):
                self._json({"error": "invalid json body"}, 400)
                return
            resp, code = perform_add_account(
                payload.get("phone"), payload.get("password"),
                str(payload.get("display_name") or ""))
            self._json(resp, code)
            return

        self._send(404, b"not found", "text/plain")

    def _do_login(self, parsed):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(min(length, 8192)).decode("utf-8")
        except (ValueError, OSError):
            body = ""
        password = parse_qs(body).get("password", [""])[0]

        global _fail_count
        if hmac.compare_digest(
                hashlib.sha256(password.encode("utf-8")).hexdigest(),
                PASSWORD_HASH):
            with _fail_lock:
                _fail_count = 0
            token = _new_session()
            self._send(302, b"", "text/plain",
                       {"Location": "/",
                        "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; "
                                      f"HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"})
            return
        # 防爆破：连续失败线性加长响应耗时（0.5s × 次数，封顶 5s）
        with _fail_lock:
            _fail_count += 1
            time.sleep(min(0.5 * _fail_count, 5.0))
        self._send(302, b"", "text/plain",
                   {"Location": "/login?error=1"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    # pythonw 下 sys.stdout 为 None，print 会抛错导致静默启动失败
    if sys.stdout is not None:
        print(f"work-cloud dashboard on http://127.0.0.1:{PORT} "
              f"(password file: {PASSWORD_FILE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
