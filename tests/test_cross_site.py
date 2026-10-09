"""跨站请求防护：只收本机/Tailscale 主机、application/json、同源 Origin 的 POST。"""
from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

import app


@pytest.fixture
def server(events):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def post(port, headers, body=b'{"hashes": []}', path="/api/translate/get"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, r.read()


def test_same_origin_json_allowed(server):
    h = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server}"}
    assert post(server, h)[0] == 200


def test_text_plain_simple_request_refused(server):
    assert post(server, {"Content-Type": "text/plain"})[0] == 403


def test_foreign_origin_refused(server):
    h = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert post(server, h)[0] == 403


def test_rebound_host_refused(server):
    h = {"Content-Type": "application/json", "Host": f"evil.example:{server}"}
    assert post(server, h)[0] == 403


def test_tailnet_host_allowed(server):
    h = {"Content-Type": "application/json; charset=utf-8", "Host": "macbox.tailabc.ts.net",
         "Origin": "https://macbox.tailabc.ts.net"}
    assert post(server, h)[0] == 200


def test_non_object_json_gets_error_response_not_dropped(server):
    status, body = post(server, {"Content-Type": "application/json"}, body=b"[1]")
    assert status == 500 and b"JSON object" in body
