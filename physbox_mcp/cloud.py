"""
PhysBox cloud access for the MCP server.

Every other tool in this package drives a browser tab over ``ws://localhost:3142``
and touches no server at all. That is the free, browser-local product, and it
works with nothing configured.

This module is the other kind: it talks straight to ``api.physbox.io`` so an agent
can read the run archive and the document library with no tab open — which is the
whole point, because the question people actually ask ("what did I cut that walnut
at in March") is about a job that finished weeks ago.

Those routes are PhysBox Pro. Nothing here decides that: the API does, and this
module's job is to carry a credential and relay a refusal in words an agent can
repeat to its user.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

#: Where the API lives when nothing says otherwise. Mirrors the frontends'
#: ``getApiBaseUrl()``, minus the localhost detection — there is no page origin
#: here to detect anything from, so a local API is named explicitly.
DEFAULT_API_URL = "https://api.physbox.io"

#: Directory name, matching what PhysBox Native already uses for its own config
#: (``%APPDATA%\\physbox-native`` / ``~/.config/physbox-native``). Somebody who has
#: found one PhysBox config directory should be able to guess the next.
CONFIG_DIR_NAME = "physbox-mcp"

CREDENTIALS_FILENAME = "credentials.json"

TOKEN_ENV_VAR = "PHYSBOX_API_TOKEN"
API_URL_ENV_VAR = "PHYSBOX_API_URL"

#: Set from the browser handshake when a tab is attached. Last resort, deliberately
#: — see ``resolve_token``.
_browser_token: str | None = None


class CloudError(RuntimeError):
    """Anything that went wrong reaching the API, in words worth relaying."""


class NoCredentials(CloudError):
    pass


class ProRequired(CloudError):
    pass


def api_base_url() -> str:
    return os.environ.get(API_URL_ENV_VAR, DEFAULT_API_URL).rstrip("/")


def credentials_path() -> Path:
    """
    Where a saved token lives, per platform.

    Windows is not an afterthought here: PhysBox Native ships Windows builds, the
    README documents WSL and Antigravity setups, and the machine on the bench is
    very often driven from Windows. ``~/.physbox`` would technically resolve there
    and be wrong twice over — the wrong convention, and invisible in Explorer.

    macOS follows the Linux path rather than ``~/Library/Application Support``:
    matching the rest of the suite is worth more than matching Apple on one
    platform.
    """
    if sys.platform == "win32":
        roaming = os.environ.get("APPDATA")
        base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
        return base / CONFIG_DIR_NAME / CREDENTIALS_FILENAME

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / CONFIG_DIR_NAME / CREDENTIALS_FILENAME


def read_credentials_file() -> dict:
    path = credentials_path()
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # unreadable or malformed is the same as absent
        print(f"Could not read {path}: {e}", file=sys.stderr)
        return {}


def write_credentials_file(token: str) -> Path:
    """
    Saves a token for next time.

    The ``chmod`` is meaningful on POSIX and a no-op on Windows, where the file
    inherits the ACL of a per-user directory instead. Worth being clear about
    rather than implying the call protected anything there.
    """
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"token": token}, f)
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return path


def set_browser_token(token: str | None) -> None:
    """Remembers the token a connected tab offered during its handshake."""
    global _browser_token
    if token and token.startswith("pbx_"):
        _browser_token = token


def resolve_token() -> str:
    """
    Finds a credential, in order of how much the user meant it.

    The environment variable is the documented path because it is the only one
    that behaves identically on Windows, WSL, macOS and Linux — and WSL is really
    two machines, with two home directories, so a file is exactly the thing that
    ends up on the wrong side of the fence.

    The browser handshake is last on purpose. The hub is unauthenticated, and a
    bearer token that arrives over it is a credential somebody else's process could
    also have offered.
    """
    env = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env:
        return env

    saved = read_credentials_file().get("token")
    if isinstance(saved, str) and saved.strip():
        return saved.strip()

    if _browser_token:
        return _browser_token

    raise NoCredentials(
        "No PhysBox credential found. Mint a read token at "
        "https://physbox.io/history.html and set it as "
        f"{TOKEN_ENV_VAR} in this server's MCP config (the `env` block), or save it to "
        f"{credentials_path()}."
    )


def _request(path: str, params: dict | None = None) -> Any:
    token = resolve_token()
    url = f"{api_base_url()}{path}"
    if params:
        # Drop the unset ones rather than sending `since=None`, which the API would
        # read as a filter on the string "None".
        query = {k: v for k, v in params.items() if v is not None and v != ""}
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            pass

        if e.code == 401:
            raise CloudError(
                "PhysBox rejected this credential. It may have been revoked — mint a new "
                "one at https://physbox.io/history.html."
            ) from None
        if e.code == 403 and body.get("code") == "pro_required":
            # Said in full, because the alternative is an agent reporting "no runs
            # found" for an account whose runs were simply never recorded.
            raise ProRequired(
                "Job history and cloud documents are a PhysBox Pro feature. This account is "
                "on the free tier, so nothing has been archived for it. "
                f"See {body.get('upgradeUrl', 'https://physbox.io/pro.html')}. "
                "The local app tools still work with no account at all."
            ) from None
        if e.code == 403 and body.get("code") == "token_read_only":
            raise CloudError("This PhysBox token is read-only.") from None
        raise CloudError(f"PhysBox API error {e.code}: {body.get('error', e.reason)}") from None
    except urllib.error.URLError as e:
        raise CloudError(f"Could not reach {api_base_url()}: {e.reason}") from None


async def get(path: str, params: dict | None = None) -> Any:
    """Reads from the API without blocking the event loop the tools run on."""
    return await asyncio.to_thread(_request, path, params)
