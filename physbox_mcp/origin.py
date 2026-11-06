"""Who may open the hub's WebSocket.

The hub listens on loopback, but a page on any site can open a socket to
localhost: a browser's same-origin policy does not cover WebSockets. Without
this, any web page open in the browser could drive a connected machine or pose
as a tab. A browser cannot lie about Origin; a local process can, but a local
process needs no web page to reach the port.
"""

import http
import re

# No Origin at all is a peer hub or a desktop app: only browsers send one.
_APP_ORIGIN = re.compile(
    r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
    r"|https://([a-z0-9-]+\.)+physbox\.io"
)


def origin_allowed(origins: list[str]) -> bool:
    """Whether a handshake carrying these Origin header values may connect."""
    if not origins:
        return True
    return len(origins) == 1 and _APP_ORIGIN.fullmatch(origins[0]) is not None


async def refuse_foreign_origin(first, second):
    """`process_request` for `websockets.serve`, whichever API that resolves to.

    websockets 14+ calls this with (connection, request); before that with
    (path, headers). The `origins=` argument only takes patterns from 15 on,
    and pyproject allows 12.
    """
    legacy = isinstance(first, str)
    headers = second if legacy else second.headers
    if origin_allowed(headers.get_all("Origin")):
        return None
    body = "This origin may not use the PhysBox MCP hub.\n"
    if legacy:
        return http.HTTPStatus.FORBIDDEN, [], body.encode()
    return first.respond(http.HTTPStatus.FORBIDDEN, body)
