import time

import pytest

import cmon


class Vault:
    """In-memory stand-in for the OS keyring."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def delete_password(self, service, account):
        del self.entries[(service, account)]

    def get_keyring(self):
        return type("Backend", (), {"name": "test vault"})()


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body or {}, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


@pytest.fixture
def auth(home, monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cmon, "LEGACY_PURGED", str(home / ".cmon" / ".legacy-chain-purged"))
    vault = Vault()
    monkeypatch.setattr(cmon, "_keyring", lambda: vault)
    creds = {"accessToken": "cc-1", "refreshToken": "cc-refresh", "expiresAt": int((time.time() + 3600) * 1000)}
    monkeypatch.setattr(cmon, "_claude_code_cred", lambda: dict(creds))
    calls = []

    class Session:
        trust_env = False

        def get(self, url, timeout, headers):
            calls.append(("GET", headers["Authorization"]))
            return responses.pop(0)

        def post(self, *a, **k):  # any refresh attempt would land here
            calls.append(("POST", a))
            raise AssertionError("cmon must never POST to the OAuth token endpoint")

    responses = []
    monkeypatch.setattr(cmon, "_session", Session)
    return vault, creds, calls, responses


def test_expired_claude_code_token_fails_fast_without_refresh(auth):
    _vault, creds, calls, _ = auth
    creds["expiresAt"] = int((time.time() - 60) * 1000)
    with pytest.raises(cmon.FetchError, match="renews the next time Claude Code runs"):
        cmon.fetch(retries=3)
    assert calls == []  # no usage call with a dead token, and no refresh POST


def test_401_rereads_claude_code_once_without_spending_a_retry(auth, monkeypatch):
    _vault, creds, calls, responses = auth
    responses += [Resp(401), Resp(200, {"limits": []})]
    original = cmon._claude_code_cred
    state = {"n": 0}

    def renewed():
        state["n"] += 1
        blob = original()
        if state["n"] > 1:  # Claude Code renewed its own login in the meantime
            blob["accessToken"] = "cc-2"
        return blob

    monkeypatch.setattr(cmon, "_claude_code_cred", renewed)
    assert cmon.fetch(retries=1) == {"limits": []}
    assert calls == [("GET", "Bearer cc-1"), ("GET", "Bearer cc-2")]


def test_401_with_unchanged_token_reports_source(auth):
    _vault, _creds, calls, responses = auth
    responses += [Resp(401)]
    with pytest.raises(cmon.FetchError, match="Claude Code credentials was rejected"):
        cmon.fetch(retries=3)
    assert len(calls) == 1


def test_legacy_refresh_chain_is_purged_once(auth):
    vault, _creds, _calls, _ = auth
    vault.entries[(cmon.SERVICE, cmon.LEGACY_AUTO_ACCOUNT)] = '{"refreshToken": "old"}'
    assert cmon._resolve_token()[:2] == (cmon.CC_SOURCE, "cc-1")
    assert (cmon.SERVICE, cmon.LEGACY_AUTO_ACCOUNT) not in vault.entries
    assert cmon.os.path.exists(cmon.LEGACY_PURGED)


def test_no_refresh_code_path_remains():
    src = open(cmon.__file__, encoding="utf-8").read()
    assert "oauth/token" not in src and "grant_type" not in src
