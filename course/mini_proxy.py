# -*- coding: utf-8 -*-
"""最小 HTTP CONNECT 代理（只服务本项目的 ECS）。

安全约束：
  * 只接受来自允许列表（默认 ECS 公网 IP）的连接，其它一律 403 并立即断开；
  * 不记录 URL/内容，只统计连接数；
  * 不落任何缓存。

用法：python3 mini_proxy.py <port>
"""
import select
import socket
import sys
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
ALLOW = {"118.190.106.170"}          # ECS 出口 IP
BUF = 65536
UPSTREAM_TIMEOUT = 20


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                d = s.recv(BUF)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except Exception:
        pass


def handle(conn, addr):
    if addr[0] not in ALLOW:
        try:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        except Exception:
            pass
        conn.close()
        return
    try:
        conn.settimeout(UPSTREAM_TIMEOUT)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            d = conn.recv(BUF)
            if not d:
                conn.close()
                return
            head += d
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2:
            conn.close()
            return
        method, target = parts[0], parts[1]
        if method.upper() != "CONNECT":
            conn.sendall(b"HTTP/1.1 405 Only CONNECT\r\n\r\n")
            conn.close()
            return
        host, _, port = target.rpartition(":")
        up = socket.create_connection((host, int(port or 443)), timeout=UPSTREAM_TIMEOUT)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        conn.settimeout(None)
        up.settimeout(None)
        t = threading.Thread(target=pipe, args=(up, conn), daemon=True)
        t.start()
        pipe(conn, up)
        up.close()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(64)
    print("mini_proxy listening on %d, allow=%s" % (PORT, sorted(ALLOW)), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
