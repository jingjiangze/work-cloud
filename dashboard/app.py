# -*- coding: utf-8 -*-
"""work-cloud 本地看板（只读）。

- 纯标准库实现（http.server），零第三方依赖，低占用；
- 只读：仅读取 data/ 下的状态/台账/风险文件，不提供任何写操作或文件列举；
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
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(ROOT)
DATA_DIR = os.path.join(PROJECT, "data")
STATIC_DIR = os.path.join(ROOT, "static")
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

_sessions: dict = {}
_sessions_lock = threading.Lock()


def _new_session() -> str:
    token = secrets.token_urlsafe(24)
    with _sessions_lock:
        # 清理过期会话
        now = datetime.now().timestamp()
        expired = [t for t, exp in _sessions.items() if exp < now]
        for t in expired:
            _sessions.pop(t, None)
        _sessions[token] = now + SESSION_TTL
    return token


def _valid_session(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
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
        if parsed.path != "/login":
            self._send(404, b"not found", "text/plain")
            return
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
