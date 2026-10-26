"""What the managed-mode clients share: one authenticated gateway connection.

Managed mode points every perception capability at the conductor-grounder
gateway instead of a model server (see ``managed_grounder`` for why the client
carries no dialect or topology). ``GatewayClient`` is that connection: the
seat token, the timeouts, and the mapping from every failure mode to a
``GrounderError`` the agent can act on.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time

import httpx

from .grounder import CoordSpace, GrounderError

log = logging.getLogger(__name__)

# 429 handling: retry this many times, waiting what Retry-After asks (or
# 1s, 2s without one). A Retry-After longer than the cap fails at once, since
# blocking a tool call that long is worse than telling the agent to slow down.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_MAX_WAIT = 10.0
_sleep = asyncio.sleep  # patched in tests


def retry_after_seconds(value: str | None) -> float | None:
    """Seconds a Retry-After header asks for: delta-seconds or an HTTP
    date. None when absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def gateway_bbox(b: object) -> tuple[int, int, int, int] | None:
    """Read a gateway ``Bbox`` into the (x1,y1,x2,y2) tuple the elements use.

    The gateway's Go ``fuzzy.Bbox`` has no JSON tags, so it marshals with
    capitalized keys (``X1``/``Y1``/``X2``/``Y2``). We accept the lower-case
    forms too, defensively, in case the wire shape ever gains tags.

    Returns None when a coordinate is missing or not a number. Defaulting it
    to 0 turned a malformed candidate into a confident click at the top-left
    corner of the screen.
    """
    if not isinstance(b, dict):
        return None
    out: list[int] = []
    for upper, lower in (("X1", "x1"), ("Y1", "y1"), ("X2", "x2"), ("Y2", "y2")):
        v = b.get(upper, b.get(lower))
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        out.append(int(v))
    return out[0], out[1], out[2], out[3]


class GatewayClient:
    """An authenticated connection to the conductor-grounder gateway.

    ``what`` names the caller in error messages ("managed grounder"), and
    ``timeout_env`` names the variable that raises its timeout.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float,
                 *, what: str, timeout_env: str,
                 http: httpx.AsyncClient | None = None) -> None:
        if http is None and not api_key:
            raise ValueError(
                f"{what} needs an API key — set CONDUCTOR_DESKTOP_API_KEY "
                f"to your cgr_live_… seat token."
            )
        self.base = base_url.rstrip("/")
        self.what = what
        self._timeout_env = timeout_env
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})

    def borrow(self) -> GatewayClient:
        """A second view on this connection, for another capability. Closing
        the borrowed one leaves the pool to its owner."""
        view = GatewayClient(self.base, "", 0, what=self.what,
                             timeout_env=self._timeout_env, http=self._http)
        return view

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _send(self, path: str, payload: dict) -> httpx.Response:
        """One POST; transport failures become a ``GrounderError``."""
        try:
            return await self._http.post(f"{self.base}{path}", json=payload)
        except httpx.TimeoutException as e:
            raise GrounderError(
                f"{self.what} timed out after {self._http.timeout.read}s "
                f"talking to {self.base} ({type(e).__name__}). The gateway is "
                f"slow or unreachable — check it, or raise {self._timeout_env}."
            ) from e
        except httpx.HTTPError as e:
            raise GrounderError(
                f"could not reach the {self.what} at {self.base} "
                f"({type(e).__name__}: {e}). Check the gateway is up and "
                f"CONDUCTOR_DESKTOP_GROUNDER_URL points at it."
            ) from e

    async def post(self, path: str, payload: dict, *,
                   allow: tuple[int, ...] = ()) -> httpx.Response:
        """POST to a gateway endpoint; every failure becomes a
        ``GrounderError`` naming the gateway and the likely fix, so callers
        that already catch ``RuntimeError`` keep working.

        Statuses in ``allow`` come back unraised: /v1/ground answers 404 for
        an honest no-match.
        """
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            r = await self._send(path, payload)
            if r.status_code != 429:
                break
            asked = retry_after_seconds(r.headers.get("retry-after"))
            wait = float(2 ** attempt) if asked is None else asked
            if attempt == RATE_LIMIT_RETRIES or wait > RATE_LIMIT_MAX_WAIT:
                after = f" (Retry-After: {asked:g}s)" if asked is not None else ""
                raise GrounderError(
                    f"{self.what} returned 429 Too Many Requests on {path}{after}"
                    f" after {attempt + 1} attempt(s) — rate limited. Slow down: "
                    f"wait before the next locate/click/read_screen, or batch "
                    f"lookups into fewer calls."
                )
            log.info("%s rate limited on %s; retrying in %.1fs", self.what, path, wait)
            await _sleep(wait)
        if r.status_code == 402:
            raise GrounderError(
                f"{self.what} returned 402 Payment Required — the credit "
                f"wallet is empty. Top up at the gateway's billing page, then "
                f"retry."
            )
        if r.status_code == 401:
            raise GrounderError(
                f"{self.what} returned 401 Unauthorized — the API key is "
                f"missing, wrong, or revoked. Check CONDUCTOR_DESKTOP_API_KEY."
            )
        if r.status_code in allow:
            return r
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise GrounderError(
                f"{self.what} at {self.base} returned HTTP "
                f"{e.response.status_code} on {path}. The gateway is up but "
                f"rejected the request — check its logs."
            ) from e
        return r

    def body(self, r: httpx.Response, path: str) -> dict:
        """The JSON object in a gateway reply, or a GrounderError saying why
        not. A proxy in front of the gateway can answer 200 with an HTML page;
        that must surface as the soft error, not a JSONDecodeError crash."""
        where = f"{self.what} at {self.base} {path}"
        try:
            data = r.json()
        except ValueError as e:
            raise GrounderError(
                f"{where} returned a non-JSON reply (HTTP {r.status_code}, "
                f"{r.headers.get('content-type', 'no content-type')}). Is something "
                f"other than the gateway answering at that URL?"
            ) from e
        if not isinstance(data, dict):
            raise GrounderError(f"{where} returned JSON {type(data).__name__}, "
                                f"expected an object")
        return data

    def coord_space(self, declared: object) -> CoordSpace:
        """The space the gateway says it answered in. Absent means its
        default, norm1000. An unknown space is an error: projecting it as
        either known space would put clicks in the wrong place."""
        if not declared:
            return CoordSpace.NORM1000
        try:
            return CoordSpace(str(declared))
        except ValueError as e:
            raise GrounderError(
                f"{self.what} served coord_system={declared!r}, which this "
                f"client can't project. Upgrade conductor-desktop, or ask the "
                f"gateway for {CoordSpace.NORM1000} coordinates."
            ) from e
