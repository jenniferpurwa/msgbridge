"""OAuth flow with a throwaway test password and authenticator secret."""
import asyncio
import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from msgbridge.auth import MAX_FAILURES, Auth, setup_credentials, totp

TEST_PASSWORD = "test-password-not-real-123"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def auth(tmp_path):
    a = Auth(tmp_path / "auth.db", tmp_path / "credentials.json", "https://msg.example.test")
    a.secret = setup_credentials(tmp_path / "credentials.json", TEST_PASSWORD)
    return a


def client(redirect=CHATGPT_REDIRECT):
    return OAuthClientInformationFull(client_id="c1", redirect_uris=[redirect], token_endpoint_auth_method="none")


def start(auth):
    c = client()
    run(auth.register_client(c))
    url = run(auth.authorize(c, AuthorizationParams(state="xyz", scopes=["messages:read"], code_challenge="abc",
                                                    redirect_uri=CHATGPT_REDIRECT, redirect_uri_provided_explicitly=True)))
    return c, parse_qs(urlparse(url).query)["request"][0]


def now_code(auth):
    return totp(auth.secret, int(time.time() // 30))


def test_only_chatgpt_and_claude_redirects(auth):
    with pytest.raises(RegistrationError):
        run(auth.register_client(client("https://evil.example.com/callback")))
    with pytest.raises(RegistrationError):
        run(auth.register_client(client("http://chatgpt.com/callback")))  # not https
    run(auth.register_client(client("https://claude.ai/api/mcp/auth_callback")))


def test_wrong_password_or_code_is_refused(auth):
    _, req = start(auth)
    assert auth.complete_login(req, "wrong", now_code(auth))[0] is None
    assert auth.complete_login(req, TEST_PASSWORD, "000000")[0] is None


def test_full_flow_issues_tokens_and_codes_are_single_use(auth):
    c, req = start(auth)
    redirect, err = auth.complete_login(req, TEST_PASSWORD, now_code(auth))
    assert err == "" and redirect.startswith(CHATGPT_REDIRECT)
    q = parse_qs(urlparse(redirect).query)
    assert q["state"] == ["xyz"]
    code = run(auth.load_authorization_code(c, q["code"][0]))
    tokens = run(auth.exchange_authorization_code(c, code))
    assert run(auth.load_access_token(tokens.access_token)).client_id == "c1"
    with pytest.raises(Exception):
        run(auth.exchange_authorization_code(c, code))  # second use
    assert auth.complete_login(req, TEST_PASSWORD, now_code(auth))[0] is None  # request consumed


def test_refresh_rotates_and_revoke_all_signs_out(auth):
    c, req = start(auth)
    redirect, _ = auth.complete_login(req, TEST_PASSWORD, now_code(auth))
    code = run(auth.load_authorization_code(c, parse_qs(urlparse(redirect).query)["code"][0]))
    tokens = run(auth.exchange_authorization_code(c, code))
    old = run(auth.load_refresh_token(c, tokens.refresh_token))
    new = run(auth.exchange_refresh_token(c, old, []))
    assert run(auth.load_refresh_token(c, tokens.refresh_token)) is None  # old refresh token is gone
    assert run(auth.load_access_token(new.access_token)) is not None
    auth.revoke_all()
    assert run(auth.load_access_token(new.access_token)) is None


def test_authenticator_code_works_once(auth):
    _, req1 = start(auth)
    code = now_code(auth)
    assert auth.complete_login(req1, TEST_PASSWORD, code)[0]
    _, req2 = start(auth)
    assert auth.complete_login(req2, TEST_PASSWORD, code)[0] is None


def test_lockout_after_repeated_failures(auth):
    _, req = start(auth)
    for _ in range(MAX_FAILURES):
        auth.complete_login(req, "wrong", "123456")
    _, err = auth.complete_login(req, TEST_PASSWORD, now_code(auth))
    assert "Too many failed attempts" in err


def test_pasted_password_matches_typed(tmp_path):
    a = Auth(tmp_path / "a.db", tmp_path / "c.json", "https://msg.example.test")
    setup_credentials(tmp_path / "c.json", "\x1b[200~" + TEST_PASSWORD + "\x1b[201~")  # pasted in a terminal
    from msgbridge.auth import check_password
    import json
    stored = json.loads((tmp_path / "c.json").read_text())["password"]
    assert check_password(TEST_PASSWORD, stored)  # typed in the browser
    assert a is not None


def test_password_reset_keeps_authenticator_key(tmp_path):
    first = setup_credentials(tmp_path / "c.json", TEST_PASSWORD)
    assert setup_credentials(tmp_path / "c.json", "another-password-456", keep_secret=True) == first
