#!/usr/bin/env python3
"""邮件 LLM 摘要与降噪模块。

从 mail_client.py 抽取的独立叶子模块，包含：LLM 调用、长邮件切片摘要、
邮件核心信息提取、静态/启发式降噪规则。

约束：本模块禁止在顶层 import mail_client（避免循环依赖）；确需 mail_client
的配置兜底时，在函数内按需延迟 import。
"""

import json
import os


def _load_llm_env_from_dotenv():
    """从候选 .env 文件加载 LLM_* 环境变量(仅当未设置时),使直接 CLI 运行也能读到密钥。

    与 oauth_helper.get_encryption_token 的候选路径一致,支持读取 ../lite_agent/.env,
    实现 lite_agent 与 mail-statement-parser 跨项目共享同一份 .env,无需重复配置。
    仅加载 LLM_* 相关变量且不覆盖已存在的环境变量(lite_agent 子进程注入优先)。
    """
    _LLM_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL")
    if all(os.environ.get(k) for k in _LLM_KEYS):
        return
    _cur = os.path.dirname(os.path.abspath(__file__))
    _candidates = [
        os.path.join(_cur, ".env"),
        os.path.join(_cur, "..", ".env"),
        os.path.join(_cur, "..", "lite_agent", ".env"),
        os.path.join(_cur, "..", "lite-agent", ".env"),
    ]
    for _env_path in _candidates:
        if not os.path.exists(_env_path):
            continue
        try:
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    if "=" not in _line or _line.strip().startswith("#"):
                        continue
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    if _k in _LLM_KEYS and not os.environ.get(_k):
                        os.environ[_k] = _v.strip().strip("'").strip('"')
        except Exception:
            pass


_load_llm_env_from_dotenv()


def _resolve_llm_pool():
    """解析 LLM 端点池。优先 LLM_POOL(JSON 列表)，否则退回单端点 env/config。返回端点 dict 列表。"""
    import os, json
    pool_raw = os.environ.get("LLM_POOL")
    if pool_raw:
        try:
            pool = json.loads(pool_raw)
            if isinstance(pool, list) and pool:
                return pool
        except Exception:
            pass
    # 退回单端点（兼容旧用法）
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    proxy = os.environ.get("LLM_PROXY", "")
    if not api_key:
        try:
            from mail_client import load_config  # 延迟导入避免循环依赖
            config = load_config()
            llm_cfg = config.get("llm", {})
            api_key = llm_cfg.get("api_key")
            if llm_cfg.get("base_url"):
                base_url = llm_cfg.get("base_url")
            if llm_cfg.get("provider"):
                provider = llm_cfg.get("provider").lower()
            if llm_cfg.get("model"):
                model = llm_cfg.get("model")
        except Exception:
            pass
    return [{"api_key": api_key, "base_url": base_url, "provider": provider, "model": model,
             "proxy": proxy}]


_OFFICIAL_GEMINI_HOST = "generativelanguage.googleapis.com"
# 需要剥离的兼容路径后缀（仅在本 host 的路径末尾命中时剥离）
_GEMINI_COMPAT_SUFFIXES = ("/v1beta/openai", "/v1beta")
# 历史兜底：provider=gemini 但 base_url 仍是 OpenAI 默认值（未配置 Gemini 地址）时指向官方主机。
# 这是既有行为，与“把任意非官方地址改为直连”无关；自定义网关一律不动。
_GEMINI_DEFAULT_FALLBACK_HOSTS = ("api.openai.com",)


def _normalize_gemini_base(base_url):
    """把 Gemini 端点的 base_url 归一化为原生 generateContent 可用的基地址。

    规则（按 host 精确判断，避免误伤自定义网关）：
      - 官方主机 generativelanguage.googleapis.com：
          * 路径以 /v1beta/openai 结尾（OpenAI 兼容层）→ 归一为 scheme://host
          * 路径以 /v1beta 结尾（官方 API 前缀）→ 归一为 scheme://host（保证不出现重复 /v1beta）
          * 其他路径 → 保留（拼接时会得到 .../v1beta/models/...）
      - api.openai.com 默认值（provider=gemini 却未配置地址）→ 官方主机（历史兜底）
      - 其他自定义网关：保留 scheme/authority/路径前缀；仅当路径以已知兼容后缀结尾时
        剥离该后缀，网关地址本身不变。
    """
    from urllib.parse import urlsplit, urlunsplit

    raw = (base_url or "").strip()
    if not raw:
        return f"https://{_OFFICIAL_GEMINI_HOST}"

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    path = (parts.path or "").rstrip("/")
    scheme = parts.scheme or "https"

    if host == _OFFICIAL_GEMINI_HOST:
        for suffix in _GEMINI_COMPAT_SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urlunsplit((scheme, parts.netloc, path, "", ""))

    if host in _GEMINI_DEFAULT_FALLBACK_HOSTS:
        return f"https://{_OFFICIAL_GEMINI_HOST}"

    for suffix in _GEMINI_COMPAT_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _curl_proxy_args(proxy):
    """Validate proxy settings, preserving credentials and IPv6 addresses.

    SOCKS5 resolves target names at the proxy, matching the production egress
    requirement. Unsupported protocols fail explicitly rather than going direct.
    """
    from urllib.parse import urlsplit, urlunsplit
    import urllib.error

    raw = (proxy or "").strip()
    if not raw:
        return []
    try:
        parts = urlsplit(raw if "://" in raw else "socks5://" + raw)
        scheme = parts.scheme.lower()
        if scheme not in ("socks5", "socks5h", "http", "https"):
            raise ValueError("unsupported proxy protocol")
        if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("invalid proxy address")
        if parts.port is not None and not 0 < parts.port <= 65535:
            raise ValueError("invalid proxy port")
        scheme = "socks5h" if scheme == "socks5" else scheme
        return ["--proxy", urlunsplit((scheme, parts.netloc, "", "", ""))]
    except ValueError as exc:
        raise urllib.error.URLError("Invalid or unsupported proxy configuration") from exc


def _curl_post_json(url, headers, body_bytes, proxy, timeout):
    """POST via configured proxy, with bounded transfer and private curl input.

    Credentials go through stdin, never process arguments. The private temporary
    directory also protects response content and is removed on every exit path.
    Redirects are rejected rather than forwarding credential headers elsewhere.
    """
    import subprocess
    import tempfile
    import urllib.error

    proxy_args = _curl_proxy_args(proxy)

    def quoted(value):
        value = str(value)
        if any(ch in value for ch in ("\r", "\n", "\x00")):
            raise urllib.error.URLError("Invalid curl configuration value")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    with tempfile.TemporaryDirectory(prefix="llm_request_") as tmp:
        req_path = os.path.join(tmp, "request.json")
        resp_path = os.path.join(tmp, "response.json")
        with open(req_path, "wb") as request_file:
            request_file.write(body_bytes)
        settings = [
            "silent", "show-error", 'request = "POST"',
            "max-time = " + quoted(timeout),
            "output = " + quoted(resp_path),
            'write-out = "%{http_code}"',
            "url = " + quoted(url),
            "data-binary = " + quoted("@" + req_path),
            # Explicit proxy must not be bypassed by inherited NO_PROXY.
            'noproxy = ""',
        ]
        for key, value in headers.items():
            settings.append("header = " + quoted(f"{key}: {value}"))
        if proxy_args:
            settings.append("proxy = " + quoted(proxy_args[1]))
        try:
            proc = subprocess.run(
                ["curl", "-q", "--config", "-"],
                input="\n".join(settings) + "\n",
                capture_output=True, text=True, timeout=timeout + 15,
            )
        except subprocess.TimeoutExpired as exc:
            raise urllib.error.URLError(f"curl timed out ({timeout}s)") from exc
        except OSError as exc:
            raise urllib.error.URLError("curl unavailable or failed to start") from exc
        # A received HTTP header is not proof that its response body completed.
        if proc.returncode != 0:
            raise urllib.error.URLError(f"curl transfer failed (rc={proc.returncode})")
        try:
            status = int((proc.stdout or "").strip())
        except ValueError as exc:
            raise urllib.error.URLError("curl returned an invalid HTTP status") from exc
        if not 100 <= status <= 599:
            raise urllib.error.URLError("curl returned no HTTP response")
        if not 200 <= status < 300:
            raise urllib.error.HTTPError(url, status, "HTTP request failed", {}, None)
        try:
            with open(resp_path, "r", encoding="utf-8") as response_file:
                return response_file.read()
        except (OSError, UnicodeError) as exc:
            raise urllib.error.URLError("Cannot read completed curl response") from exc


def _open_request(req, proxy, timeout):
    """发请求：配置了代理就走 curl(--socks5-hostname)，否则保持 urllib 直连。"""
    import urllib.request

    if not (proxy or "").strip():
        return urllib.request.urlopen(req, timeout=timeout)

    url = req.full_url
    headers = {k: v for k, v in req.headers.items()}
    body = _curl_post_json(url, headers, req.data or b"", proxy, timeout)
    return _FakeResponse(body)


class _FakeResponse:
    """把 curl 返回的文本包装成 urlopen 响应（仅 call_llm 用到的 read/上下文协议）。"""

    def __init__(self, text):
        self._data = text.encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _build_llm_request(endpoint, prompt, system_instruction, json_mode):
    """根据单个端点构造 urllib Request。返回 (req, provider)。"""
    import urllib.request, json
    api_key = endpoint.get("api_key")
    base_url = endpoint.get("base_url", "https://api.openai.com/v1")
    provider = endpoint.get("provider", "openai").lower()
    model = endpoint.get("model", "gpt-4o-mini")

    if provider == "gemini":
        # 上游（如 lite_agent 的 LLM_POOL）可能注入 OpenAI 兼容格式的 base_url
        # （.../v1beta/openai），原生 generateContent 端点必须先剥离兼容后缀，
        # 否则拼出 .../v1beta/openai/v1beta/models/... 必然 404，端点池降级随之失效。
        # 原生 REST 分支（SDK 不可用时的兜底）：鉴权走 Gemini 自有的
        # x-goog-api-key 头，不是 OpenAI 的 Authorization: Bearer。
        base_url = _normalize_gemini_base(base_url)
        url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        generation_config = {}
        if json_mode:
            generation_config["responseMimeType"] = "application/json"
        if generation_config:
            payload["generationConfig"] = generation_config
        req_headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    else:
        url = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "temperature": 0.1}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    data_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method='POST')
    return req, provider


def _parse_llm_response(resp_data, provider):
    """解析 OpenAI 兼容 / Gemini 响应，返回文本。"""
    if provider == "gemini":
        try:
            return resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as e:
            raise ValueError(f"Failed to parse Gemini Response: {resp_data}. {e}")
    else:
        try:
            return resp_data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise ValueError(f"Failed to parse OpenAI Response: {resp_data}. {e}")


def call_llm(prompt: str, system_instruction: str = None, json_mode: bool = False) -> str:
    """使用 urllib 发送请求给 OpenAI 兼容 API 或 Gemini API。
    支持 LLM_POOL 端点轮换：遇 429/5xx/连接错误自动切换下一个端点，全尽才抛错。
    无 LLM_POOL 时退回单端点（向后兼容）。"""
    import urllib.request, urllib.error, json

    pool = _resolve_llm_pool()
    if not pool or not pool[0].get("api_key"):
        raise ValueError("LLM API key not found. Please set LLM_API_KEY/LLM_POOL env or config 'llm' block.")

    last_err = None
    for endpoint in pool:
        provider = (endpoint.get("provider") or "openai").lower()
        proxy = endpoint.get("proxy") or ""
        try:
            req, provider = _build_llm_request(endpoint, prompt, system_instruction, json_mode)
            with _open_request(req, proxy, 120) as response:
                resp_data = json.loads(response.read().decode('utf-8'))
            return _parse_llm_response(resp_data, provider)
        except urllib.error.HTTPError as e:
            last_err = e
            # 429/5xx 视为端点侧限流/故障，轮换下一个；其余 4xx 多为请求本身问题，直接抛
            if e.code == 429 or e.code >= 500:
                print(f"⚠️ LLM 端点 {endpoint.get('model')} 返回 {e.code}，切换下一个池端点...")
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            print(f"⚠️ LLM 端点 {endpoint.get('model')} 连接失败: {e}，切换下一个池端点...")
            continue
    raise last_err if last_err else RuntimeError("LLM pool exhausted with no error")


def slice_and_summarize_long_email(body_text: str, max_chunk_len: int = 30000) -> str:
    """若邮件内容过长，对内容进行切片（Slicing），分段提取摘要并拼接，防止直接截断丢失核心信息。"""
    if len(body_text) <= max_chunk_len:
        return body_text

    print(f"📝 邮件正文过长 ({len(body_text)} 字符)，启动分段切片摘要提取...")

    chunks = []
    start = 0
    total_len = len(body_text)

    while start < total_len:
        if start + max_chunk_len >= total_len:
            chunks.append(body_text[start:])
            break

        end_idx = start + max_chunk_len
        search_min = start + 3500
        boundary_idx = -1

        # 寻找最近的换行符
        for idx in range(end_idx - 1, search_min - 1, -1):
            if body_text[idx] == '\n':
                boundary_idx = idx
                break

        # 如果没有找到换行符，寻找句子终结符
        if boundary_idx == -1:
            for idx in range(end_idx - 1, search_min - 1, -1):
                if body_text[idx] in ('.', '。', '?', '？', '!', '！'):
                    boundary_idx = idx + 1
                    break

        if boundary_idx != -1:
            chunk = body_text[start:boundary_idx].strip()
            start = boundary_idx
        else:
            chunk = body_text[start:end_idx].strip()
            start = end_idx

        chunks.append(chunk)

    summaries = []
    max_chunks = 5
    truncated = len(chunks) > max_chunks

    for idx, chunk in enumerate(chunks[:max_chunks]):
        prompt = (
            f"以下是一封超长邮件的第 {idx+1}/{len(chunks)} 分块内容。请在 150 字内精确提取本段内容的关键核心信息、时间限制或待办事项：\n\n"
            f"{chunk}"
        )
        system_instruction = (
            "你是一个精炼的文本分段摘要提取助手。\n"
            "⚠️ 注意：你必须在摘要中特别【原样保留】任何具体的日期时间（如“下周二之前”、“2026-07-15”）、"
            "验证码/验证令牌、金额数字及重要专有名词，绝对不能对其进行任何转述、泛化或省略！"
        )
        try:
            summary = call_llm(prompt, system_instruction=system_instruction)
            summaries.append(f"[分块 {idx+1} 核心摘要]: {summary}")
        except Exception as e:
            print(f"⚠️ 分块 {idx+1} 摘要提取失败: {e}")
            summaries.append(f"[分块 {idx+1} 截取]: {chunk[:200]}...")

    result = "\n\n".join(summaries)
    if truncated:
        result += f"\n\n⚠️ [警告]: 邮件内容过长 (已超 {max_chunk_len * max_chunks} 字符限制)，尾部内容已被自动截断。"

    return result


def extract_email_summary_by_llm(subject: str, sender: str, body_text: str, email_date: str) -> dict:
    """借助大模型分析邮件提取核心分类与摘要信息。"""
    from datetime import datetime
    import json
    current_time_str = datetime.now().isoformat()

    # 启用长邮件切片与预提炼
    sliced_body = slice_and_summarize_long_email(body_text)

    system_instruction = (
        "你是一个电子邮件处理网关。请阅读下面的邮件，并输出一份完全符合指定 JSON 约束格式的内容。\n"
        "不允许有任何代码块标记（如 ```json）或额外的包裹描述，必须直接输出合法的 JSON 对象本身。\n\n"
        "JSON 输出约束格式如下：\n"
        "{\n"
        "  \"category\": \"Work\" | \"Finance\" | \"Security\" | \"Personal\" | \"Newsletter\" | \"Spam\",\n"
        "  \"importance\": \"high\" | \"medium\" | \"low\",\n"
        "  \"summary\": \"一至两句精确的中文字幕核心摘要。\",\n"
        "  \"actions\": [\"动作项描述1\", \"动作项描述2\"],\n"
        "  \"deadline\": \"YYYY-MM-DD HH:MM\" | null,\n"
        "  \"deadline_raw\": \"截止时间在邮件中的最原始文本描述\" | null\n"
        "}\n\n"
        "注意事项：\n"
        "1. category 分类：'Work'(工作计划、会议通知)、'Finance'(退款、发票、除了银行月度对账单以外的财务往来)、'Security'(验证码、登录风险提醒)、'Personal'(个人联络)、'Newsletter'(周报、新闻、日常订阅订阅)、'Spam'(垃圾推广/广告)\n"
        "2. importance 分级：'high'(含明确重要时间限制的通知、账号密码等安全修改、财务重大变动)、'medium'(普通通知、日常会议安排)、'low'(常规无须回馈的报告、推广)\n"
        "3. deadline 计算：必须以“当前系统时间”和邮件的“发件时间”为准，将邮件中的相对表述（如：下周一下午）换算为归一化 ISO 绝对日期时间，若没有则填 null。"
    )

    prompt = (
        f"当前系统时间 (基准参考): {current_time_str}\n"
        f"发信时间: {email_date}\n"
        f"发件人: {sender}\n"
        f"主题: {subject}\n\n"
        f"正文内容:\n{sliced_body}"
    )

    response_text = call_llm(prompt, system_instruction=system_instruction, json_mode=True)

    if response_text.startswith("```"):
        lines = response_text.splitlines()
        if len(lines) >= 2:
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                response_text = "\n".join(lines[1:-1])

    response_text = response_text.strip()
    return json.loads(response_text)


def load_static_noise_rules() -> dict:
    """载入 noise_rules.json，包含 whitelists 和 spam_keywords 等"""
    default_rules = {
        "white_domains": ["bank", "secure", "security", "aliyun", "tencent", "github", "paypal", "stripe", "microsoft", "google", "apple"],
        "white_local_prefixes": ["verify", "code", "order", "receipt", "bill", "alert", "notify", "support", "service"],
        "spam_keywords": ["广告", "优惠券", "促销", "特惠", "打折", "限时特购", "双十一", "爆款", "推广", "理财推荐"],
        "sensitive_words": ["账单", "订单", "安全", "密码", "验证码", "异常", "登录", "verify", "code", "security", "password", "alert", "login", "billing"],
        "protected_domains": ["qq.com", "gmail.com", "163.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]
    }

    current_dir = os.path.dirname(os.path.abspath(__file__))
    rules_path = os.path.join(current_dir, "noise_rules.json")
    if os.path.exists(rules_path):
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 无法加载 noise_rules.json: {e}，将使用内置默认规则。")

    return default_rules


def is_noise_email(sender: str, subject: str, body_text: str, db_path: str) -> tuple[bool, str, str]:
    """启发式邮件前置过滤，拦截无须大模型消费的明显的自动通知。

    db_path: 动态黑名单库所在的 statements.db 路径（原为 mail_client.DB_PATH 全局，
    抽取后改为显式参数，避免叶子模块依赖 mail_client 的全局状态）。
    """
    from email.utils import parseaddr
    sender_l = sender.lower()
    subject_l = subject.lower()

    _, addr = parseaddr(sender_l)
    addr_l = addr.lower()
    local, domain = addr_l.split('@') if '@' in addr_l else (addr_l, '')

    # 1. 载入静态过滤规则
    rules = load_static_noise_rules()
    white_domains = rules.get("white_domains", [])
    white_local_prefixes = rules.get("white_local_prefixes", [])
    spam_keywords = rules.get("spam_keywords", [])
    sensitive_words = rules.get("sensitive_words", [])

    # 2. 显式的白名单放行（绝对优先）：安全告警、验证码、交易订单确认、重要资产
    is_whitelisted = False
    if any(d in domain for d in white_domains):
        is_whitelisted = True
    elif any(local.startswith(p) or f".{p}" in local or f"_{p}" in local or local == p for p in white_local_prefixes):
        is_whitelisted = True

    if is_whitelisted:
        return False, '', ''

    # 3. 动态黑名单库校验
    from statement_db import load_noise_rules
    dynamic_rules = load_noise_rules(db_path)
    for p_type, p_val in dynamic_rules:
        p_val_l = p_val.lower()
        if p_type == 'sender_domain' and p_val_l == domain:
            return True, 'Newsletter', 'low'
        elif p_type == 'sender_email' and p_val_l == addr_l:
            return True, 'Newsletter', 'low'

    # 4. 垃圾推广与促销广告 (Spam)
    if any(k in subject_l for k in spam_keywords):
        return True, 'Spam', 'low'

    # 5. 显式的邮件订阅与 Newsletter
    body_l = body_text.lower()
    has_sensitive = any(w in subject_l or w in body_l for w in sensitive_words)
    if ('unsubscribe' in body_l or '退订' in body_text) and not has_sensitive:
        return True, 'Newsletter', 'low'

    # 仅针对明确是自动日报/周报/订阅号的 sender 进行拦截
    noise_senders = ['newsletter', 'weekly-report', 'daily-report', 'digest', 'weekly-digest']
    if any(s in sender_l for s in noise_senders):
        return True, 'Newsletter', 'low'

    return False, '', ''
