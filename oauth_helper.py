#!/usr/bin/env python3
"""Standard library OAuth 2.0 authentication and encryption helper for POP3/IMAP/SMTP."""

import os
import sys
import json
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import time
from typing import Optional, Dict

# 强制终端标准输出为 UTF-8 编码，防止 Windows 平台 GBK 编码报错
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# Token 文件存储路径
TOKEN_FILE_TEMPLATES = {
    "gmail": "token_gmail.json",
    "outlook": "token_outlook.json"
}

# ==========================================
# 密码学安全标准库加密 (PBKDF2 + HMAC-CTR)
# ==========================================

def get_encryption_token() -> str:
    """获取对称密钥派生源 API_AUTH_TOKEN。优先读 env，次之读相对路径候选，最末兜底。"""
    token = os.environ.get("API_AUTH_TOKEN")
    if token:
        return token
        
    current_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(current_dir, ".env"),
        os.path.join(current_dir, "..", ".env"),
        os.path.join(current_dir, "..", "lite_agent", ".env"),
        os.path.join(current_dir, "..", "lite-agent", ".env"),
    ]
    
    for env_path in candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if "=" in line and not line.strip().startswith("#"):
                            k, v = line.split("=", 1)
                            if k.strip() == "API_AUTH_TOKEN":
                                return v.strip().strip("'").strip('"')
            except Exception:
                pass
            
    return "API_AUTH_TOKEN_DEFAULT_BACKUP_KEY_2026"


def _derive_key(api_token: str, salt: bytes) -> bytes:
    # PBKDF2: 随机 salt + 600,000 次 SHA256 迭代派生 32 字节密钥
    return hashlib.pbkdf2_hmac('sha256', api_token.encode(), salt, 600_000, dklen=32)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    # HMAC-CTR: 为每个 32 字节 Block 产生唯一密钥流
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(16, 'big'), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_token_data(plaintext: bytes, api_token: str) -> bytes:
    """利用 PBKDF2 密钥派生和 HMAC-CTR 对明文进行加密，并在最外层进行 HMAC 完整性校验 (Encrypt-then-MAC)。"""
    salt, nonce = os.urandom(16), os.urandom(16)
    key = _derive_key(api_token, salt)
    ct = bytes(p ^ k for p, k in zip(plaintext, _keystream(key, nonce, len(plaintext))))
    tag = hmac.new(key, salt + nonce + ct, hashlib.sha256).digest()
    return salt + nonce + tag + ct


def decrypt_token_data(blob: bytes, api_token: str) -> bytes:
    """对加密 blob 进行完整性 compare_digest 校验并解密。若校验失败则抛出安全异常。"""
    if len(blob) < 64:
        raise ValueError("密文大小非法")
    salt, nonce, tag, ct = blob[:16], blob[16:32], blob[32:64], blob[64:]
    key = _derive_key(api_token, salt)
    
    # 恒定时间比较防时序攻击
    if not hmac.compare_digest(tag, hmac.new(key, salt + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("token 密文已被篡改，或 API_AUTH_TOKEN 不匹配")
        
    return bytes(c ^ k for c, k in zip(ct, _keystream(key, nonce, len(ct))))


# ==========================================
# 统一网络 HTTP 请求助手 (0 外部依赖)
# ==========================================

def _http_post(url: str, params: dict, proxy: str = None) -> dict:
    import socket
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    
    orig_socket = socket.socket
    try:
        if proxy:
            import socks
            host, port = proxy.split(":")
            socks.set_default_proxy(socks.SOCKS5, host, int(port))
            socket.socket = socks.socksocket
            
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            return json.loads(error_body)
        except Exception:
            raise e
    finally:
        socket.socket = orig_socket

# ==========================================
# OAuth 2.0 刷新与授权核心业务
# ==========================================

def load_encrypted_token(provider: str) -> Optional[dict]:
    """从磁盘加载解密后的 Token 字典。"""
    filename = TOKEN_FILE_TEMPLATES.get(provider.lower())
    if not filename or not os.path.exists(filename):
        return None
        
    api_token = get_encryption_token()
    try:
        with open(filename, "rb") as f:
            blob = f.read()
        plaintext = decrypt_token_data(blob, api_token)
        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        print(f"⚠️ 无法解密加载 {filename}: {e}")
        return None


def save_encrypted_token(provider: str, token_data: dict) -> None:
    """对 Token 字典加密并写入磁盘，设置 owner 只读权限。"""
    filename = TOKEN_FILE_TEMPLATES.get(provider.lower())
    if not filename:
        raise ValueError(f"不支持的 provider: {provider}")
        
    api_token = get_encryption_token()
    plaintext = json.dumps(token_data).encode('utf-8')
    blob = encrypt_token_data(plaintext, api_token)
    
    # 写入文件
    with open(filename, "wb") as f:
        f.write(blob)
        
    # Enforce Owner-only permissions (chmod 600) on Unix/Mac systems
    if os.name == 'posix':
        try:
            os.chmod(filename, 0o600)
        except Exception:
            pass


def get_valid_oauth_token(email_config: dict) -> str:
    """获取当前可用的 OAuth2 Access Token。若过期则自动静默刷新。"""
    provider = email_config.get('provider', '').lower()
    token_data = load_encrypted_token(provider)
    
    if not token_data:
        raise ValueError(f"❌ 账号 {email_config.get('account')} 未进行 OAuth2 授权。请在终端执行 'python oauth_helper.py authorize {provider}' 完成认证。")
        
    now = time.time()
    # 提前 60 秒判定过期以防传输临界失效
    expires_at = token_data.get('created_at', 0) + token_data.get('expires_in', 3600)
    if now < expires_at - 60:
        return token_data['access_token']
        
    # 触发静默刷新
    print(f"🔄 OAuth2 令牌过期，正在对 {provider} 执行静默刷新...")
    refresh_token = token_data.get('refresh_token')
    if not refresh_token:
        raise ValueError(f"❌ Token 缓存中缺失 refresh_token。请重新执行 'python oauth_helper.py authorize {provider}'。")
        
    client_id = email_config.get('client_id')
    client_secret = email_config.get('client_secret')
    
    if not client_id:
        raise ValueError("❌ 配置文件中缺失 client_id。OAuth2 模式必须在配置中填写 client_id。")
        
    try:
        if provider == 'gmail':
            # Google 刷新接口
            url = "https://oauth2.googleapis.com/token"
            params = {
                "client_id": client_id,
                "client_secret": client_secret or "",
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
        elif provider == 'outlook':
            # 微软 /consumers 个人版刷新接口
            url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
            params = {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
        else:
            raise ValueError(f"不支持的 OAuth2 Provider: {provider}")
            
        res = _http_post(url, params, proxy=email_config.get("imap_proxy"))
        if "error" in res:
            raise ValueError(f"刷新失败: {res.get('error_description', res.get('error'))}")
            
        # 更新缓存，保留原 refresh_token (如果新响应中没有返回)
        token_data['access_token'] = res['access_token']
        token_data['expires_in'] = res.get('expires_in', 3600)
        token_data['created_at'] = int(time.time())
        if 'refresh_token' in res:
            token_data['refresh_token'] = res['refresh_token']
            
        save_encrypted_token(provider, token_data)
        print("✅ OAuth2 令牌刷新成功。")
        return token_data['access_token']
    except Exception as e:
        raise ValueError(f"❌ OAuth2 令牌自动刷新失败: {e}。请重新授权以恢复服务。")

# ==========================================
# CLI 交互式授权引导 (Headless 友好)
# ==========================================

def run_outlook_device_flow(client_id: str):
    """微软设备码授权流 (头部无需浏览器，对 VPS 极其友好)"""
    device_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
    # 需要 Microsoft Graph 的邮件读写和发送权限
    scopes = "offline_access Mail.Read Mail.Send"
    
    print("🔑 [Microsoft OAuth] 正在向 Azure 请求设备验证码...")
    res = _http_post(device_url, {
        "client_id": client_id,
        "scope": scopes
    })
    
    if "error" in res:
        print(f"❌ 请求失败: {res.get('error_description', res.get('error'))}")
        return
        
    device_code = res['device_code']
    user_code = res['user_code']
    verification_uri = res['verification_uri']
    interval = res.get('interval', 5)
    
    print("\n=======================================================")
    print(f"👉 请在您的 PC 或手机浏览器上打开此链接:\n   {verification_uri}")
    print(f"👉 输入以下设备确认码:\n   【 {user_code} 】")
    print("=======================================================\n")
    print("⏳ 等待用户在浏览器端完成授权认证...")
    
    token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    while True:
        time.sleep(interval)
        try:
            token_res = _http_post(token_url, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code
            })
        except (urllib.error.URLError, OSError):
            continue
        
        if "error" in token_res:
            err = token_res["error"]
            if err == "authorization_pending":
                continue
            elif err == "authorization_declined":
                print("❌ 授权被拒绝。")
                break
            elif err == "expired_token":
                print("❌ 设备确认码已过期，请重新运行。")
                break
            else:
                print(f"❌ 授权异常: {token_res.get('error_description', err)}")
                break
        else:
            # 授权成功
            token_res['created_at'] = int(time.time())
            save_encrypted_token("outlook", token_res)
            print("\n🎉 [Microsoft] Outlook 邮箱 OAuth2 授权成功！密钥已安全加密落库。")
            break


def run_gmail_web_flow(client_id: str, client_secret: str):
    """谷歌 Desktop app 本地回环授权流 (无头主机通过拷贝 redirect_uri 的 URL code 完成校验)"""
    auth_base = "https://accounts.google.com/o/oauth2/v2/auth"
    redirect_uri = "http://localhost:8080"
    scope = "https://mail.google.com/"
    
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent"
    }
    
    auth_url = f"{auth_base}?{urllib.parse.urlencode(params)}"
    
    print("\n=======================================================")
    print("👉 请在您的 PC 浏览器中打开以下授权链接:")
    print(f"   {auth_url}")
    print("=======================================================\n")
    print("ℹ️ 授权完成后，浏览器由于无头主机原因会提示连接失败。")
    print("ℹ️ 请从您的浏览器【地址栏】中复制跳转后的完整 URL（包含 ?code=...）并粘贴到下方：")
    
    try:
        user_input = input("\n📥 请输入重定向 URL 或 Code: ").strip()
    except KeyboardInterrupt:
        print("\n❌ 操作被取消。")
        return
        
    code = user_input
    if "code=" in user_input:
        try:
            parsed = urllib.parse.urlparse(user_input)
            query = urllib.parse.parse_qs(parsed.query)
            if "code" in query:
                code = query["code"][0]
        except Exception:
            pass
            
    if not code:
        print("❌ 输入格式非法，无法提取授权码。")
        return
        
    print("🔑 正在向 Google 换取 Access & Refresh Tokens...")
    token_url = "https://oauth2.googleapis.com/token"
    res = _http_post(token_url, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    })
    
    if "error" in res:
        print(f"❌ 换取 Token 失败: {res.get('error_description', res.get('error'))}")
        return
        
    res['created_at'] = int(time.time())
    save_encrypted_token("gmail", res)
    print("\n🎉 [Google] Gmail 邮箱 OAuth2 授权成功！密钥已安全加密落库。")


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "authorize":
        print("用法：")
        print("  python oauth_helper.py authorize outlook")
        print("  python oauth_helper.py authorize gmail")
        sys.exit(1)
        
    provider = sys.argv[2].lower()
    
    # 动态载入配置读取 client_id/secret
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        from mail_client import load_config
        config = load_config()
    except Exception as e:
        print(f"⚠️ 无法读取配置文件，尝试查找当前目录下的 json: {e}")
        # fallback 手动扫描
        config = {}
        for candidate in ["email-config.local.json", "email-config.json"]:
            if os.path.exists(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        config = json.load(f)
                        break
                except Exception:
                    pass
                    
    # 优先从新的 emails 数组匹配对应 provider，找不到再看旧的 email 块
    client_id = None
    client_secret = None
    if "emails" in config and isinstance(config["emails"], list):
        for acc in config["emails"]:
            if acc.get("provider") == provider:
                client_id = acc.get("client_id")
                client_secret = acc.get("client_secret")
                break
    
    if not client_id and "email" in config:
        email_block = config.get("email", {})
        client_id = email_block.get("client_id")
        client_secret = email_block.get("client_secret")
    
    if provider == "outlook":
        if not client_id:
            print("❌ 错误：请先在 email-config.local.json 的 email 块中配置 client_id。")
            sys.exit(1)
        run_outlook_device_flow(client_id)
    elif provider == "gmail":
        if not client_id or not client_secret:
            print("❌ 错误：请先在 email-config.local.json 的 email 块中配置 client_id 和 client_secret。")
            sys.exit(1)
        run_gmail_web_flow(client_id, client_secret)
    else:
        print(f"❌ 未知提供商: {provider}。仅支持 outlook 或 gmail。")
        sys.exit(1)


if __name__ == "__main__":
    main()
