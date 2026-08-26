"""Minimal HTTP + SSE client for a local TrueForge server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import httpx

DEFAULT_BASE_URL = "http://localhost:8790"


class TrueForgeError(RuntimeError):
    pass


def _timeouts(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=read_seconds, write=30.0, pool=10.0)


@dataclass
class TrueForgeClient:
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    read_timeout: float = 900.0
    _http: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers=headers,
            timeout=_timeouts(self.read_timeout),
        )

    def __enter__(self) -> "TrueForgeClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise TrueForgeError(f"{method} {path} -> {response.status_code}: {response.text[:600]}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def health(self) -> bool:
        try:
            self._request("GET", "/api/v1/agents")
        except (TrueForgeError, httpx.HTTPError):
            return False
        return True

    def list_agents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/agents").get("data", [])

    def create_agent(self, name: str, manifest: dict[str, Any]) -> dict[str, Any]:
        body = self._request("POST", "/api/v1/agents", json={"name": name, "manifest": manifest})
        return body.get("data", body)

    def update_agent(self, agent_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        body = self._request("PUT", f"/api/v1/agents/{agent_id}", json={"manifest": manifest})
        return body.get("data", body)

    def upsert_agent(self, name: str, manifest: dict[str, Any]) -> tuple[str, str]:
        existing = next((a for a in self.list_agents() if a.get("name") == name), None)
        if existing is not None:
            self.update_agent(existing["id"], manifest)
            return existing["id"], "updated"
        created = self.create_agent(name, manifest)
        return created["id"], "created"

    def list_mcp_tools(self, server_name: str) -> list[dict[str, Any]]:
        candidates = (
            f"/api/v1/mcp-servers/{server_name}/tools",
            f"/api/v1/settings/mcp-servers/{server_name}/tools",
        )
        last_error: Exception | None = None
        for path in candidates:
            try:
                body = self._request("GET", path)
            except (TrueForgeError, httpx.HTTPError) as exc:
                last_error = exc
                continue
            data = body.get("data", body)
            if isinstance(data, dict):
                data = data.get("tools", [])
            return list(data or [])
        raise TrueForgeError(f"Could not list tools for {server_name!r}: {last_error}")

    def create_session(self, agent_name: str) -> dict[str, Any]:
        body = self._request("POST", "/api/v1/sessions", json={"agent": {"name": agent_name}})
        data = body.get("data", {})
        if "id" not in data:
            raise TrueForgeError(f"create_session: unexpected response shape (no 'id' in data): {body}")
        return data

    def stream_turn(self, session_id: str, input_items: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        payload = {"input": list(input_items), "stream": True}
        path = f"/api/v1/sessions/{session_id}/turns"
        try:
            with self._http.stream("POST", path, json=payload, headers={"Accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    response.read()
                    raise TrueForgeError(f"POST {path} -> {response.status_code}: {response.text[:600]}")
                buffer: list[str] = []
                for raw_line in response.iter_lines():
                    line = raw_line.rstrip("\r")
                    if line.startswith(":"):
                        continue
                    if line:
                        if line.startswith("data:"):
                            buffer.append(line[len("data:"):].lstrip())
                        continue
                    event = _decode(buffer)
                    buffer = []
                    if event is not None:
                        yield event
                trailing = _decode(buffer)
                if trailing is not None:
                    yield trailing
        except TrueForgeError:
            raise
        except httpx.HTTPError as exc:
            raise TrueForgeError(f"stream_turn network error: {exc}") from exc


def _decode(buffer: list[str]) -> dict[str, Any] | None:
    if not buffer:
        return None
    chunk = "\n".join(buffer).strip()
    if not chunk or chunk == "[DONE]":
        return None
    try:
        decoded = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None
