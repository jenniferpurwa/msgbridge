"""OAuth 2.1 for the web server: one account (you), password plus a one-time code.

ChatGPT (or Claude) registers itself, sends you to /login, and after you sign in
gets a 1-hour access token and a 30-day refresh token. Only redirects back to
ChatGPT or Claude are accepted, tokens are stored as hashes, and repeated
failed sign-ins lock the login for 15 minutes.

Credentials are created by `msgbridge auth setup`, which you run yourself in a
terminal: the password never passes through anything else.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPES = ["messages:read", "offline_access"]
ALLOWED_REDIRECT_HOSTS = {"chatgpt.com", "chat.openai.com", "claude.ai", "claude.com"}
ACCESS_TTL, REFRESH_TTL, CODE_TTL, LOGIN_TTL = 3600, 30 * 86400, 300, 600
MAX_FAILURES, LOCKOUT = 5, 15 * 60

SCHEMA = """
create table if not exists client(client_id text primary key, info text not null, created_at real not null);
create table if not exists pending(id text primary key, client_id text not null, params text not null, expires_at real not null);
create table if not exists code(hash text primary key, data text not null, expires_at real not null);
create table if not exists token(hash text primary key, kind text not null, client_id text not null,
                                 scopes text not null, expires_at real not null);
create table if not exists failure(at real not null);
create table if not exists totp_used(counter integer primary key);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- credentials ------------------------------------------------------------------
PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")  # terminals wrap pasted text in these


def clean_password(password: str) -> str:
    """The same password typed or pasted, in a terminal or the browser, must compare equal."""
    for marker in PASTE_MARKERS:
        password = password.replace(marker, "")
    return password.strip("\r\n")


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(clean_password(password).encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return {"salt": salt.hex(), "hash": digest.hex()}


def check_password(password: str, stored: dict) -> bool:
    candidate = hash_password(password, bytes.fromhex(stored["salt"]))["hash"]
    return hmac.compare_digest(candidate, stored["hash"])


def totp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def setup_credentials(path: Path, password: str, keep_secret: bool = False) -> str:
    """Store a password hash and an authenticator secret (new, or the current one); returns the secret."""
    secret = (json.loads(path.read_text())["totp_secret"] if keep_secret and path.exists()
              else base64.b32encode(os.urandom(20)).decode())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"password": hash_password(password), "totp_secret": secret}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return secret


class Auth:
    """Implements the MCP SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, db_path: Path, credentials_path: Path, public_url: str):
        self.public_url = public_url.rstrip("/")
        self.credentials_path = credentials_path
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        os.chmod(db_path, 0o600)
        self.lock = threading.Lock()

    def _exec(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params).fetchall()

    # -- clients ------------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self._exec("select info from client where client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            u = urlparse(str(uri))
            if u.scheme != "https" or u.hostname not in ALLOWED_REDIRECT_HOSTS:
                raise RegistrationError(error="invalid_redirect_uri",
                                        error_description="msgbridge only accepts ChatGPT and Claude redirect URLs")
        self._exec("insert or replace into client values (?, ?, ?)",
                   (client_info.client_id, client_info.model_dump_json(), time.time()))

    # -- authorization ------------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.credentials_path.exists():
            raise AuthorizeError(error="server_error", error_description="Run `msgbridge auth setup` first.")
        request_id = secrets.token_urlsafe(24)
        self._exec("insert into pending values (?, ?, ?, ?)",
                   (request_id, client.client_id, params.model_dump_json(), time.time() + LOGIN_TTL))
        return f"{self.public_url}/login?request={request_id}"

    def login_page(self, request_id: str, message: str = "") -> str:
        note = f'<p class="err">{html.escape(message)}</p>' if message else ""
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>msgbridge sign in</title>
<style>body{{font:16px -apple-system,system-ui,sans-serif;max-width:360px;margin:12vh auto;padding:0 16px}}
input{{display:block;width:100%;box-sizing:border-box;margin:6px 0 14px;padding:10px;font:inherit}}
button{{padding:10px 16px;font:inherit}}.err{{color:#b00020}}</style></head><body>
<h1>msgbridge</h1><p>Sign in to let this assistant read your messages (read-only).</p>{note}
<form method="post" action="/login"><input type="hidden" name="request" value="{html.escape(request_id)}">
<label>Password<input type="password" name="password" autocomplete="current-password" required></label>
<label>Authenticator code<input name="code" inputmode="numeric" pattern="[0-9]{{6}}" autocomplete="one-time-code" required></label>
<button type="submit">Allow read-only access</button></form></body></html>"""

    def _locked_out(self) -> bool:
        since = time.time() - LOCKOUT
        self._exec("delete from failure where at < ?", (since,))
        return self._exec("select count(*) from failure")[0][0] >= MAX_FAILURES

    def _check_totp(self, secret: str, code: str) -> bool:
        now = int(time.time() // 30)
        for counter in (now - 1, now, now + 1):
            if hmac.compare_digest(totp(secret, counter), code.strip()):
                used = self._exec("select 1 from totp_used where counter = ?", (counter,))
                if used:
                    return False  # each code works once
                self._exec("insert into totp_used values (?)", (counter,))
                self._exec("delete from totp_used where counter < ?", (now - 10,))
                return True
        return False

    def complete_login(self, request_id: str, password: str, code: str) -> tuple[str | None, str]:
        """(redirect URL, "") on success, or (None, error message)."""
        rows = self._exec("select client_id, params, expires_at from pending where id = ?", (request_id,))
        if not rows or rows[0][2] < time.time():
            return None, "This sign-in link expired. Start again from ChatGPT or Claude."
        if self._locked_out():
            return None, "Too many failed attempts. Try again in 15 minutes."
        creds = json.loads(self.credentials_path.read_text())
        if not (check_password(password, creds["password"]) and self._check_totp(creds["totp_secret"], code)):
            self._exec("insert into failure values (?)", (time.time(),))
            return None, "Wrong password or code."
        client_id, params_json, _ = rows[0]
        params = AuthorizationParams.model_validate_json(params_json)
        self._exec("delete from pending where id = ?", (request_id,))
        auth_code = secrets.token_urlsafe(32)
        data = AuthorizationCode(code=auth_code, scopes=params.scopes or SCOPES, expires_at=time.time() + CODE_TTL,
                                 client_id=client_id, code_challenge=params.code_challenge,
                                 redirect_uri=params.redirect_uri,
                                 redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                 resource=params.resource, subject="me")
        self._exec("insert into code values (?, ?, ?)", (_hash(auth_code), data.model_dump_json(), data.expires_at))
        return construct_redirect_uri(str(params.redirect_uri), code=auth_code, state=params.state), ""

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        rows = self._exec("select data, expires_at from code where hash = ?", (_hash(authorization_code),))
        if not rows or rows[0][1] < time.time():
            return None
        code = AuthorizationCode.model_validate_json(rows[0][0])
        return code if code.client_id == client.client_id else None

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self._exec("insert into token values (?, 'access', ?, ?, ?)", (_hash(access), client_id, " ".join(scopes), now + ACCESS_TTL))
        self._exec("insert into token values (?, 'refresh', ?, ?, ?)", (_hash(refresh), client_id, " ".join(scopes), now + REFRESH_TTL))
        self._exec("delete from token where expires_at < ?", (now,))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        deleted = self._exec("delete from code where hash = ? returning hash", (_hash(authorization_code.code),))
        if not deleted:
            raise TokenError(error="invalid_grant", error_description="Code already used.")
        return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        rows = self._exec("select client_id, scopes, expires_at from token where hash = ? and kind = 'refresh'",
                          (_hash(refresh_token),))
        if not rows or rows[0][2] < time.time() or rows[0][0] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=rows[0][0], scopes=rows[0][1].split(),
                            expires_at=int(rows[0][2]), subject="me")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        self._exec("delete from token where hash = ?", (_hash(refresh_token.token),))  # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        rows = self._exec("select client_id, scopes, expires_at from token where hash = ? and kind = 'access'",
                          (_hash(token),))
        if not rows or rows[0][2] < time.time():
            return None
        return AccessToken(token=token, client_id=rows[0][0], scopes=rows[0][1].split(),
                           expires_at=int(rows[0][2]), subject="me")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._exec("delete from token where hash = ?", (_hash(token.token),))

    def diagnose(self, password: str, code: str) -> list[str]:
        """For `msgbridge auth check`: says which part is wrong without using up the code."""
        creds = json.loads(self.credentials_path.read_text())
        lines = [f"Password: {'correct' if check_password(password, creds['password']) else 'WRONG'}"]
        now = int(time.time() // 30)
        offsets = [d for d in range(-20, 21) if hmac.compare_digest(totp(creds["totp_secret"], now + d), code.strip())]
        if not offsets:
            lines.append("Code: WRONG - it doesn't match this setup key at any nearby time. Your authenticator "
                         "probably has a key from a different `auth setup` run; run setup again and replace the entry.")
        elif abs(offsets[0]) <= 1:
            lines.append("Code: correct")
        else:
            lines.append(f"Code: matches, but {offsets[0] * 30:+d} s off. Check the clock on your phone or Mac.")
        failures = self._exec("select count(*) from failure where at > ?", (time.time() - LOCKOUT,))[0][0]
        lines.append(f"Recent failed sign-ins: {failures} of {MAX_FAILURES} before a 15-minute lockout")
        return lines

    def clear_failures(self) -> None:
        self._exec("delete from failure")

    def revoke_all(self) -> int:
        n = self._exec("select count(*) from token")[0][0]
        self._exec("delete from token")
        self._exec("delete from code")
        self._exec("delete from pending")
        return n
