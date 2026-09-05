"""
Tests for the cloud client — the one part of this server that is pure logic.

Everything else here drives a browser over a socket and is awkward to test without
one. `cloud.py` is different: it resolves a credential, builds a URL and turns an
HTTP status into a sentence, and every one of those has a wrong answer that would
only ever show up on somebody else's machine.

Which is most of the point of the platform tests below. A path helper exercised only
on Linux is a path helper that is wrong on Windows, and Windows is where a lot of
these machines are actually driven from.
"""

import asyncio
import io
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

from physbox_mcp import cloud

#: Captured before the autouse fixture below replaces it, so the two tests that
#: genuinely want to touch the filesystem can put the real one back.
REAL_READ_CREDENTIALS = cloud.read_credentials_file


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No ambient credential, and no memory of one from a previous test."""
    monkeypatch.delenv(cloud.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(cloud.API_URL_ENV_VAR, raising=False)
    monkeypatch.setattr(cloud, "_browser_token", None)
    monkeypatch.setattr(cloud, "read_credentials_file", lambda: {})
    yield


# ── Where the credential lives ────────────────────────────────────────────────

def test_windows_uses_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\tom\AppData\Roaming")

    path = cloud.credentials_path()

    assert path.parts[-2:] == ("physbox-mcp", "credentials.json")
    # Matches what PhysBox Native already does, rather than a dotfile in the user's
    # home directory that Explorer will not show them.
    assert "Roaming" in str(path)
    assert ".config" not in str(path)


def test_windows_falls_back_when_appdata_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    path = cloud.credentials_path()

    assert path.parts[-4:] == ("AppData", "Roaming", "physbox-mcp", "credentials.json")


def test_posix_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert cloud.credentials_path() == tmp_path / "cfg" / "physbox-mcp" / "credentials.json"


def test_posix_defaults_to_dot_config(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    path = cloud.credentials_path()

    assert path.parts[-3:] == (".config", "physbox-mcp", "credentials.json")


def test_macos_follows_the_suite_not_apple(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # Deliberate: matching physbox-native across every platform beats matching
    # Apple's convention on one of them.
    assert "Application Support" not in str(cloud.credentials_path())
    assert ".config/physbox-mcp" in str(cloud.credentials_path()).replace("\\", "/")


def test_writes_and_reads_back_a_token(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cloud, "read_credentials_file", REAL_READ_CREDENTIALS)

    path = cloud.write_credentials_file("pbx_written")

    assert json.loads(path.read_text())["token"] == "pbx_written"
    assert cloud.read_credentials_file()["token"] == "pbx_written"
    # `sys.platform` is faked above, but the filesystem underneath is not: on real
    # NTFS a chmod cannot clear the group/other bits, so only assert the mode where
    # it means something. On Windows the per-user directory's ACL is what protects it.
    if os.name == "posix":
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_an_unreadable_credentials_file_is_the_same_as_none(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cloud, "read_credentials_file", REAL_READ_CREDENTIALS)
    path = cloud.credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")

    assert cloud.read_credentials_file() == {}


# ── Which credential wins ─────────────────────────────────────────────────────

def test_environment_beats_everything(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_from_env")
    monkeypatch.setattr(cloud, "read_credentials_file", lambda: {"token": "pbx_from_file"})
    cloud.set_browser_token("pbx_from_browser")

    assert cloud.resolve_token() == "pbx_from_env"


def test_file_beats_the_browser(monkeypatch):
    monkeypatch.setattr(cloud, "read_credentials_file", lambda: {"token": "pbx_from_file"})
    cloud.set_browser_token("pbx_from_browser")

    assert cloud.resolve_token() == "pbx_from_file"


def test_the_browser_is_the_last_resort():
    cloud.set_browser_token("pbx_from_browser")

    # Last on purpose: the hub it arrives over has no origin check and no shared
    # secret, so anything on the machine could have offered it.
    assert cloud.resolve_token() == "pbx_from_browser"


def test_the_browser_token_must_look_like_one():
    cloud.set_browser_token("not-a-physbox-token")

    with pytest.raises(cloud.NoCredentials):
        cloud.resolve_token()


def test_no_credential_says_what_to_do(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(cloud.NoCredentials) as e:
        cloud.resolve_token()

    message = str(e.value)
    assert cloud.TOKEN_ENV_VAR in message
    assert "history.html" in message
    # The file path too, because on WSL the answer is usually "the other side".
    assert "credentials.json" in message


def test_api_url_can_be_pointed_at_a_local_server(monkeypatch):
    assert cloud.api_base_url() == "https://api.physbox.io"
    monkeypatch.setenv(cloud.API_URL_ENV_VAR, "http://localhost:3000/")
    assert cloud.api_base_url() == "http://localhost:3000"


# ── Talking to the API ────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def stub_urlopen(monkeypatch, payload=None, error=None):
    """Captures the request the client built, and answers it however asked."""
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return FakeResponse(payload)

    monkeypatch.setattr(cloud.urllib.request, "urlopen", fake)
    return seen


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.physbox.io/api/runs",
        code,
        "error",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


def test_sends_the_token_as_a_bearer(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    seen = stub_urlopen(monkeypatch, payload={"runs": []})

    assert cloud._request("/api/runs") == {"runs": []}
    assert seen["auth"] == "Bearer pbx_abc"
    assert seen["url"] == "https://api.physbox.io/api/runs"


def test_drops_unset_filters(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    seen = stub_urlopen(monkeypatch, payload={})

    cloud._request("/api/runs", {"q": "walnut", "since": None, "status": "", "limit": 50})

    # `since=None` would be read as a filter on the literal string "None".
    assert "since" not in seen["url"]
    assert "status" not in seen["url"]
    assert "q=walnut" in seen["url"]
    assert "limit=50" in seen["url"]


def test_a_free_account_is_told_the_archive_never_recorded(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    stub_urlopen(
        monkeypatch,
        error=http_error(403, {"code": "pro_required", "upgradeUrl": "https://physbox.io/pro.html"}),
    )

    with pytest.raises(cloud.ProRequired) as e:
        cloud._request("/api/runs")

    message = str(e.value)
    # The failure mode this guards against: an agent reporting "no runs found" for
    # an account whose runs were simply never being archived.
    assert "never" in message.lower() or "nothing has been archived" in message.lower()
    assert "pro.html" in message
    assert "local app tools still work" in message


def test_a_revoked_token_says_so(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    stub_urlopen(monkeypatch, error=http_error(401, {"error": "Unauthorized"}))

    with pytest.raises(cloud.CloudError) as e:
        cloud._request("/api/runs")

    assert "revoked" in str(e.value)


def test_a_read_only_token_says_so(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    stub_urlopen(monkeypatch, error=http_error(403, {"code": "token_read_only"}))

    with pytest.raises(cloud.CloudError) as e:
        cloud._request("/api/runs")

    assert "read-only" in str(e.value)


def test_other_errors_carry_the_api_message(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    stub_urlopen(monkeypatch, error=http_error(404, {"error": "No such run on this account."}))

    with pytest.raises(cloud.CloudError) as e:
        cloud._request("/api/runs/run_nope")

    assert "No such run" in str(e.value)


def test_an_unreachable_api_names_the_host(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    monkeypatch.setenv(cloud.API_URL_ENV_VAR, "http://localhost:3000")
    stub_urlopen(monkeypatch, error=urllib.error.URLError("Connection refused"))

    with pytest.raises(cloud.CloudError) as e:
        cloud._request("/api/runs")

    assert "localhost:3000" in str(e.value)


def test_get_awaits_the_same_request(monkeypatch):
    monkeypatch.setenv(cloud.TOKEN_ENV_VAR, "pbx_abc")
    stub_urlopen(monkeypatch, payload={"ok": True})

    # `asyncio.run` rather than a pytest-asyncio marker: one test needing an event
    # loop is not worth a plugin dependency in a package whose whole runtime is two
    # libraries.
    assert asyncio.run(cloud.get("/api/tokens/whoami")) == {"ok": True}
