"""mail_llm 端点池：Gemini 原生调用、代理出口与降级的回归测试。

背景（三条既有事实，缺一条都会让池里的 gemini 端点失效）：
1. lite_agent 注入的 gemini 端点 base_url 可能是 OpenAI 兼容格式
   (https://generativelanguage.googleapis.com/v1beta/openai)，原生
   generateContent 必须先剥离该后缀，否则拼出 .../v1beta/openai/v1beta/models/...
   必然 404。
2. Gemini 原生 API 不是 OpenAI 兼容格式：鉴权走自有头 x-goog-api-key，
   不是 Authorization: Bearer，也不是 /chat/completions 报文。
3. vps1 到 Google 直连不可达，出网必须走代理；Python 的 socks 组件在该环境不通，
   代理请求一律交给 curl --socks5-hostname（与 oauth_helper._http_post 同写法）。

约束：归一化只针对官方主机的已知兼容路径；自定义 Gemini 网关的
scheme/authority/路径前缀必须原样保留，不得被改写成直连官方。
"""
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import mail_llm


def _capture(endpoint, timeout=120):
    """构造请求并捕获 (url, headers)（不发网络请求）。"""
    captured = {}

    def fake_urlopen(req, timeout=120):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        raise AssertionError("不应真正发起网络请求")

    with patch.dict(os.environ, {"LLM_POOL": json.dumps([endpoint])}):
        with patch.object(urllib.request, "urlopen", fake_urlopen):
            try:
                mail_llm.call_llm("ping")
            except AssertionError:
                pass
    return captured


def _capture_url(base_url, model="gemini-2.5-flash"):
    endpoint = {"api_key": "k", "base_url": base_url, "provider": "gemini", "model": model}
    return _capture(endpoint)["url"]


EXPECTED_OFFICIAL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


class TestGeminiBaseUrlNormalization(unittest.TestCase):
    def test_openai_compat_suffix_is_stripped(self):
        self.assertEqual(_capture_url("https://generativelanguage.googleapis.com/v1beta/openai"), EXPECTED_OFFICIAL)

    def test_openai_compat_suffix_with_trailing_slash(self):
        self.assertEqual(_capture_url("https://generativelanguage.googleapis.com/v1beta/openai/"), EXPECTED_OFFICIAL)

    def test_official_v1beta_prefix_is_normalized(self):
        """官方 /v1beta 前缀也不应拼出重复的 /v1beta。"""
        self.assertEqual(_capture_url("https://generativelanguage.googleapis.com/v1beta"), EXPECTED_OFFICIAL)

    def test_native_base_url_unchanged(self):
        self.assertEqual(_capture_url("https://generativelanguage.googleapis.com"), EXPECTED_OFFICIAL)

    def test_custom_gateway_is_preserved(self):
        """自定义 Gemini 网关：scheme/authority/路径前缀原样保留。"""
        self.assertEqual(
            _capture_url("https://relay.example/gemini"),
            "https://relay.example/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        )

    def test_custom_gateway_strips_only_compat_suffix(self):
        """自定义网关自身带兼容后缀时：只剥后缀，网关地址不变。"""
        self.assertEqual(
            _capture_url("https://relay.example/gemini/v1beta/openai"),
            "https://relay.example/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        )


class TestGeminiNativeAuthHeader(unittest.TestCase):
    def test_gemini_uses_x_goog_api_key_not_bearer(self):
        """Gemini 原生调用：自有头 x-goog-api-key，不带 OpenAI 的 Authorization。"""
        cap = _capture({"api_key": "k", "base_url": "https://generativelanguage.googleapis.com",
                        "provider": "gemini", "model": "gemini-2.5-flash"})
        self.assertEqual(cap["headers"].get("X-goog-api-key"), "k")
        self.assertNotIn("Authorization", cap["headers"])

    def test_openai_endpoint_keeps_bearer(self):
        cap = _capture({"api_key": "k", "base_url": "https://api.deepseek.com/v1",
                        "provider": "openai", "model": "deepseek-v4-flash"})
        self.assertEqual(cap["headers"].get("Authorization"), "Bearer k")
        self.assertNotIn("X-goog-api-key", cap["headers"])


class TestProxyEgress(unittest.TestCase):
    """代理出口：配了 proxy 就必须走 curl，绝不能静默直连。"""

    def test_socks_proxy_uses_curl_socks5_hostname(self):
        """代理请求最终落到 curl --socks5-hostname <host:port>（不写 socks5:// 原样串）。"""
        import subprocess

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["config"] = kwargs["input"]
            from pathlib import Path
            import re
            output = re.search(r'output = "([^"]+)"', kwargs["input"]).group(1)
            captured["output"] = output
            Path(output).write_text('{}')
            return subprocess.CompletedProcess(cmd, 0, stdout="200", stderr="")

        with patch.object(subprocess, "run", fake_run):
            mail_llm._curl_post_json("https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
                                     {"Content-Type": "application/json", "x-goog-api-key": "k"},
                                     b'{"contents":[]}', "socks5://127.0.0.1:18988", 10)

        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "curl")
        self.assertEqual(cmd, ["curl", "-q", "--config", "-"])
        self.assertIn('proxy = "socks5h://127.0.0.1:18988"', captured["config"])
        self.assertIn("x-goog-api-key: k", captured["config"])
        self.assertFalse(os.path.exists(captured["output"]))

    def test_configured_proxy_never_falls_back_to_direct(self):
        """_open_request 在配置了 proxy 时必须走 curl，不得直连 urlopen。"""
        with patch.object(mail_llm, "_curl_post_json", return_value="{}") as curl:
            with patch.object(urllib.request, "urlopen") as direct:
                try:
                    mail_llm._open_request(
                        urllib.request.Request("https://x/y", data=b"{}"), "socks5://127.0.0.1:18988", 10)
                except Exception:
                    pass
        self.assertTrue(curl.called, "配置了代理却没走 curl")
        self.assertFalse(direct.called, "配置了代理仍在直连")

    def test_curl_proxy_args_scheme(self):
        self.assertEqual(
            mail_llm._curl_proxy_args("socks5://127.0.0.1:18988"),
            ["--proxy", "socks5h://127.0.0.1:18988"],
        )
        self.assertEqual(mail_llm._curl_proxy_args(""), [])

    def test_no_proxy_uses_direct_urlopen(self):
        with patch.object(mail_llm, "_curl_post_json") as curl:
            with patch.object(urllib.request, "urlopen") as direct:
                try:
                    mail_llm._open_request(urllib.request.Request("https://x/y"), "", 10)
                except Exception:
                    pass
        self.assertFalse(curl.called, "未配置代理却走了 curl")
        self.assertTrue(direct.called)

    def test_curl_error_maps_to_urlerror(self):
        """curl 无法完成（代理不可达/超时）→ URLError，交给池轮换而不是静默失败。"""
        import subprocess
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("curl", 1)):
            with self.assertRaises(urllib.error.URLError):
                mail_llm._curl_post_json("https://x/y", {}, b"{}", "socks5://127.0.0.1:18988", 10)


class TestGeminiPoolFailover(unittest.TestCase):
    def test_primary_429_falls_back_to_gemini_success(self):
        """主端点 429 后 Gemini 端点成功返回：验证真实降级路径（mock，无网络）。"""
        primary = {"api_key": "p", "base_url": "https://api.deepseek.com/v1",
                   "provider": "openai", "model": "deepseek-v4-flash"}
        backup = {"api_key": "b", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                  "provider": "gemini", "model": "gemini-2.5-flash"}
        calls = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=120):
            calls.append(req.full_url)
            if "deepseek" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
            return _Resp({"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

        with patch.dict(os.environ, {"LLM_POOL": json.dumps([primary, backup])}):
            with patch.object(urllib.request, "urlopen", fake_urlopen):
                result = mail_llm.call_llm("ping")

        self.assertEqual(result, "OK")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], EXPECTED_OFFICIAL)


if __name__ == "__main__":
    unittest.main()


class TestCurlFailureBoundaries(unittest.TestCase):
    def test_proxy_credentials_ipv6_and_protocols(self):
        for proxy, expected in [
            ("socks5://user:p%40ss@[::1]:18988", "socks5h://user:p%40ss@[::1]:18988"),
            ("socks5h://localhost:1080", "socks5h://localhost:1080"),
            ("https://user:pass@proxy.example:8443", "https://user:pass@proxy.example:8443"),
        ]:
            with self.subTest(proxy=proxy):
                self.assertEqual(mail_llm._curl_proxy_args(proxy), ["--proxy", expected])
        for proxy in ("socks4://localhost:1080", "socksbad://localhost:1080", "socks5://"):
            with self.subTest(proxy=proxy), self.assertRaises(urllib.error.URLError):
                mail_llm._curl_proxy_args(proxy)

    def test_partial_200_transfer_fails_over(self):
        import subprocess
        endpoints = [
            {"api_key": "a", "provider": "gemini", "base_url": "", "model": "m",
             "proxy": "socks5://localhost:1080"},
            {"api_key": "b", "provider": "openai", "base_url": "https://backup.invalid", "model": "n"},
        ]
        with patch.dict(os.environ, {"LLM_POOL": json.dumps(endpoints)}), \
             patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 28, "200", "timeout")), \
             patch.object(urllib.request, "urlopen", return_value=mail_llm._FakeResponse(
                 json.dumps({"choices": [{"message": {"content": "backup OK"}}]}))) as backup:
            self.assertEqual(mail_llm.call_llm("ping"), "backup OK")
            backup.assert_called_once()

    def test_http_errors_and_redirect_are_not_success(self):
        import subprocess
        for status in (302, 401, 429, 503):
            with self.subTest(status=status), \
                 patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(status), "")), \
                 self.assertRaises(urllib.error.HTTPError) as caught:
                mail_llm._curl_post_json("https://example.invalid", {}, b"{}", "http://localhost:8080", 1)
            self.assertEqual(caught.exception.code, status)

    def test_missing_curl_fails_without_direct_fallback(self):
        import subprocess
        with patch.object(subprocess, "run", side_effect=FileNotFoundError), \
             patch.object(urllib.request, "urlopen") as direct, \
             self.assertRaises(urllib.error.URLError):
            mail_llm._open_request(urllib.request.Request("https://example.invalid", data=b"{}"),
                                   "http://localhost:8080", 1)
        direct.assert_not_called()
