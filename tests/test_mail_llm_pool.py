"""mail_llm 端点池 URL 构造回归测试。

背景：lite_agent 注入的 gemini 端点 base_url 为 OpenAI 兼容格式
(https://generativelanguage.googleapis.com/v1beta/openai)，而原生
generateContent 调用必须剥离该后缀，否则拼出 .../v1beta/openai/v1beta/models/...
必然 404，端点池降级（429/5xx 切换备用端点）形同虚设。
"""
import json
import os
import unittest
import urllib.request
from unittest.mock import patch

import mail_llm


class TestGeminiBaseUrlNormalization(unittest.TestCase):
    def _url(self, base_url):
        endpoint = {"api_key": "k", "base_url": base_url, "provider": "gemini", "model": "gemini-2.5-flash"}

        captured = {}

        def fake_urlopen(req, timeout=120):
            captured["url"] = req.full_url
            raise AssertionError("不应真正发起网络请求")

        env = {"LLM_POOL": json.dumps([endpoint])}
        with patch.dict(os.environ, env):
            with patch.object(urllib.request, "urlopen", fake_urlopen):
                with self.assertRaises(AssertionError):
                    mail_llm.call_llm("ping")
        return captured["url"]

    def test_openai_compat_suffix_is_stripped(self):
        url = self._url("https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k")

    def test_openai_compat_suffix_with_trailing_slash(self):
        url = self._url("https://generativelanguage.googleapis.com/v1beta/openai/")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k")

    def test_native_base_url_unchanged(self):
        url = self._url("https://generativelanguage.googleapis.com")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k")


if __name__ == "__main__":
    unittest.main()
