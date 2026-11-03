"""本地网页服务的跨站防护。

回归 2026-11-03：服务只绑 127.0.0.1，但对请求来源**零校验**，且不论
Content-Type 一律按 JSON 解析请求体。于是用户正在浏览的任何网页都能用
no-cors 的 text/plain 简单请求（浏览器不预检）直接调本机接口——实测一条
请求就让 /api/test 把 config.json 里的 API Key 发到了对方指定的地址；
/api/open 则能让 os.startfile 打开本机任意程序。
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import src.config as _config
import webui


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "FAKE-KEY"}), encoding="utf-8")
    monkeypatch.setattr(_config, "CONFIG_PATH", cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def catcher():
    """冒充攻击者的服务器：记下收到的 Authorization 头。"""
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append(self.headers.get("Authorization"))
            self.send_response(500)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], got
    srv.shutdown()
    srv.server_close()


def _req(port, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            is_json = "json" in (r.headers.get("Content-Type") or "")
            return r.status, json.loads(raw or b"{}") if is_json else {}
    except urllib.error.HTTPError as e:
        return e.code, {}


OWN = {"X-Paper-Translator": "1", "Content-Type": "application/json"}


def test_cross_site_simple_request_cannot_steal_key(server, catcher):
    cport, got = catcher
    code, _ = _req(server, "/api/test",
                   {"base_url": f"http://127.0.0.1:{cport}"},
                   {"Content-Type": "text/plain;charset=UTF-8",
                    "Origin": "https://evil.example"})
    assert code == 403
    assert got == [], "Key 不得被发往请求方指定的地址"


def test_missing_api_header_rejected_even_same_origin(server):
    code, _ = _req(server, "/api/config", None, {})
    assert code == 403


def test_foreign_origin_rejected_even_with_header(server):
    code, _ = _req(server, "/api/config", None,
                   {**OWN, "Origin": "https://evil.example"})
    assert code == 403


def test_dns_rebinding_host_rejected(server):
    """攻击者域名解析到 127.0.0.1 后算"同源"，只有 Host 头露馅。"""
    code, _ = _req(server, "/api/config", None,
                   {**OWN, "Host": f"evil.example:{server}"})
    assert code == 403


def test_own_page_works(server):
    code, cfg = _req(server, "/api/config", None, OWN)
    assert code == 200 and cfg["api_key"] == "FAKE-KEY"
    code, _ = _req(server, "/", None, {})       # 页面本身只校验 Host
    assert code == 200
    # 浏览器对同源 POST 也会带 Origin——自己的页面必须放行
    code, _ = _req(server, "/api/jobs/nope/cancel", {},
                   {**OWN, "Origin": f"http://127.0.0.1:{server}"})
    assert code == 404, "应放行并走到路由（无此任务 → 404），而不是 403"
    code, _ = _req(server, "/api/config", None,
                   {**OWN, "Host": f"localhost:{server}"})
    assert code == 200, "用 localhost 打开页面也要能用"


def test_open_only_produced_files(server, tmp_path, monkeypatch):
    """/api/open 只许打开本程序的产物，不得成为"运行任意程序"的口子。"""
    opened = []
    monkeypatch.setattr(webui, "_os_open", opened.append)
    out = tmp_path / "paper_translation.pdf"
    out.write_bytes(b"%PDF-1.4")
    other = tmp_path / "evil.exe"
    other.write_bytes(b"MZ")
    monkeypatch.setitem(webui.JOBS, "j1", {
        "status": "done", "percent": 100, "message": "", "log": [],
        "outputs": [{"file": str(out)}], "cancel": threading.Event()})

    code, _ = _req(server, "/api/open", {"path": str(other)}, OWN)
    assert code == 403 and opened == []

    code, _ = _req(server, "/api/open", {"path": str(out)}, OWN)
    assert code == 200 and opened == [str(out.resolve())]

    code, _ = _req(server, "/api/open", {"path": str(out), "folder": True}, OWN)
    assert code == 200 and opened[-1] == str(out.resolve().parent)
