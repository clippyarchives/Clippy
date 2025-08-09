# tiktok_api.py
import os
import json
import time
import base64
import hashlib
import secrets
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
import requests

TOKENS_FILE = os.path.join(os.path.expanduser("~"), ".video916_tiktok_tokens.json")

AUTH_BASE = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USERINFO_URL = "https://open.tiktokapis.com/v2/user/info/"
# Content posting (subject to app scopes and approval)
UPLOAD_INIT = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
PUBLISH_DIRECT = "https://open.tiktokapis.com/v2/post/publish/video/"  # requires video.publish scope

SCOPES_DEFAULT = [
    "user.info.basic",
    "video.upload",
]


def _load_tokens():
    if os.path.isfile(TOKENS_FILE):
        with open(TOKENS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_tokens(data):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair():
    verifier = _b64url(secrets.token_bytes(32))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = _b64url(digest)
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    result = {"code": None, "state": None}

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]
        _CallbackHandler.result = {"code": code, "state": state}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>Authenticated. You can close this window.</body></html>")

    def log_message(self, format, *args):
        return


def oauth_connect(client_key: str, client_secret: str | None, scopes=None, redirect_port: int = 8765) -> dict:
    scopes = scopes or SCOPES_DEFAULT
    redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"
    verifier, challenge = _pkce_pair()
    state = _b64url(secrets.token_bytes(16))

    params = {
        "client_key": client_key,
        "scope": " ".join(scopes),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_BASE + "?" + urlencode(params)

    server = HTTPServer(("127.0.0.1", redirect_port), _CallbackHandler)

    def run_server():
        server.handle_request()

    th = threading.Thread(target=run_server, daemon=True)
    th.start()
    webbrowser.open(url)

    timeout = time.time() + 300
    while time.time() < timeout:
        if _CallbackHandler.result.get("code"):
            break
        time.sleep(0.2)

    server.server_close()

    code = _CallbackHandler.result.get("code")
    got_state = _CallbackHandler.result.get("state")
    if not code or got_state != state:
        raise RuntimeError("OAuth failed or cancelled")

    data = {
        "client_key": client_key,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.text}")
    tok = resp.json()
    entry = {
        "client_key": client_key,
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_in": int(tok.get("expires_in", 0)),
        "obtained_at": int(time.time()),
        "scopes": scopes,
    }
    store = _load_tokens()
    store[client_key] = entry
    _save_tokens(store)
    return entry


def _get_token(client_key: str) -> dict | None:
    store = _load_tokens()
    return store.get(client_key)


def ensure_token(client_key: str) -> dict:
    tok = _get_token(client_key)
    if not tok:
        raise RuntimeError("Not connected to TikTok")
    # Basic expiry check
    if tok.get("obtained_at", 0) + tok.get("expires_in", 0) - 60 <= int(time.time()):
        data = {
            "client_key": client_key,
            "grant_type": "refresh_token",
            "refresh_token": tok.get("refresh_token"),
        }
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Refresh failed: {resp.text}")
        nt = resp.json()
        tok.update({
            "access_token": nt.get("access_token"),
            "refresh_token": nt.get("refresh_token", tok.get("refresh_token")),
            "expires_in": int(nt.get("expires_in", tok.get("expires_in", 3600))),
            "obtained_at": int(time.time()),
        })
        store = _load_tokens()
        store[client_key] = tok
        _save_tokens(store)
    return tok


def get_user_info(client_key: str) -> dict:
    tok = ensure_token(client_key)
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    resp = requests.get(USERINFO_URL, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"User info failed: {resp.text}")
    return resp.json()


def upload_video(client_key: str, video_path: str, caption: str | None = None, direct_publish: bool = False) -> dict:
    tok = ensure_token(client_key)
    headers = {"Authorization": f"Bearer {tok['access_token']}"}

    init_body = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": os.path.getsize(video_path),
        }
    }
    resp = requests.post(UPLOAD_INIT, headers=headers, json=init_body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Upload init failed: {resp.text}")
    data = resp.json()
    upload_url = data.get("data", {}).get("upload_url")
    publish_id = data.get("data", {}).get("publish_id")
    if not upload_url or not publish_id:
        raise RuntimeError("Upload init missing upload_url/publish_id")

    with open(video_path, "rb") as fh:
        put = requests.put(upload_url, data=fh, timeout=600)
    if put.status_code not in (200, 201):
        raise RuntimeError(f"Upload PUT failed: {put.text}")

    if direct_publish:
        body = {
            "publish_id": publish_id,
            "post_info": {
                "title": caption or "",
            }
        }
        pub = requests.post(PUBLISH_DIRECT, headers=headers, json=body, timeout=30)
        if pub.status_code != 200:
            raise RuntimeError(f"Direct publish failed: {pub.text}")
        return pub.json()

    return {"publish_id": publish_id}


