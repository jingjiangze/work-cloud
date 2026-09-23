# -*- coding: utf-8 -*-
"""work-cloud 本地看板（只读）。

- 纯标准库实现（http.server），零第三方依赖，低占用；
- 只读：仅读取 data/ 下的状态/台账/风险文件，不提供任何写操作或文件列举；
- 绑定 127.0.0.1，公网暴露一律经 Cloudflare Tunnel；
- 访问控制：首次启动自动生成访问密钥 dashboard/secret_key.txt，
  浏览器通过 /login?key=xxx 换取 HttpOnly Cookie；/health 免认证供探针。
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(ROOT)
DATA_DIR = os.path.join(PROJECT, "data")
STATIC_DIR = os.path.join(ROOT, "static")
KEY_FILE = os.path.join(ROOT, "secret_key.txt")
COOKIE_NAME = "wk_dashboard"
SESSION_TTL = 12 * 3600

PORT = int(os.environ.get("WK_DASHBOARD_PORT", "8792"))

# ---------- 访问密钥 ----------

def get_access_key() -> str:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_urlsafe(18)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    return key


ACCESS_KEY = get_access_key()
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
            key = parse_qs(parsed.query).get("key", [""])[0]
            if hmac.compare_digest(key, ACCESS_KEY):
                token = _new_session()
                self._send(302, b"", "text/plain",
                           {"Location": "/",
                            "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; "
                                          f"HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"})
            else:
                self._send(401, "访问密钥错误".encode("utf-8"),
                           "text/plain; charset=utf-8")
            return

        if not self._authed():
            self._send(401,
                       "未授权。请通过 /login?key=访问密钥 登录。".encode("utf-8"),
                       "text/plain; charset=utf-8")
            return

        if path == "/" or path == "/index.html":
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

        if path == "/api/history":
            date = parse_qs(parsed.query).get("date",
                                              [datetime.now().strftime("%Y-%m-%d")])[0]
            self._json({"date": date,
                        "history": _load_history(date),
                        "risk": _load_risk(date)})
            return

        self._send(404, b"not found", "text/plain")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"work-cloud dashboard on http://127.0.0.1:{PORT} "
          f"(access key: {KEY_FILE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
