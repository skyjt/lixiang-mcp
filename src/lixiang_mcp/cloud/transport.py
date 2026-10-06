"""Bounded HTTP client; no arbitrary host forwarding, redirects, retries, or ambient proxies."""

from __future__ import annotations

from typing import Any

import httpx

ID = "https://id.lixiang.com"
ACCOUNT = "https://account.lixiang.com"
API = "https://api-app.lixiang.com"


class ProtocolError(Exception):
    """Code only: never retain response, request, cookies, identity, or transport exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CloudHTTP:
    def __init__(
        self,
        origins: frozenset[str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15,
    ) -> None:
        self.origins = origins
        self.client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=timeout
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        content: bytes | None = None,
        json: Any = None,
    ) -> httpx.Response:
        target = httpx.URL(url)
        origin = f"{target.scheme}://{target.host}"
        if origin not in self.origins or target.port not in (None, 443) or target.userinfo:
            raise ProtocolError("forbidden_upstream_origin")
        try:
            async with self.client.stream(
                method, url, headers=headers, data=data, content=content, json=json
            ) as response:
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 1_048_576:
                        raise ProtocolError("upstream_response_too_large")
                # aiter_bytes already decoded Content-Encoding. Do not decompress again or
                # advertise compressed byte lengths when constructing the detached response.
                decoded_headers = httpx.Headers(response.headers)
                for header in ("content-encoding", "content-length", "transfer-encoding"):
                    decoded_headers.pop(header, None)
                # A new response avoids retaining the original request with secret headers.
                return httpx.Response(
                    response.status_code, headers=decoded_headers, content=bytes(payload)
                )
        except httpx.InvalidURL:
            # HTTPX can parse a Location even with follow_redirects=False.
            raise ProtocolError("invalid_upstream_redirect") from None
        except httpx.HTTPError:
            raise ProtocolError("upstream_network_error") from None

    async def close(self) -> None:
        await self.client.aclose()


def object_body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise ProtocolError("invalid_upstream_json") from None
    if not isinstance(data, dict):
        raise ProtocolError("invalid_upstream_shape")
    return data
