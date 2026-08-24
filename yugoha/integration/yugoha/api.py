from __future__ import annotations
import asyncio
import json
import urllib.request
import urllib.error

class YuGoHAApi:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    async def health(self) -> dict:
        return await asyncio.to_thread(self._health_sync)

    def _health_sync(self) -> dict:
        req = urllib.request.Request(f"{self.url}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    async def send(self, message: str, title: str, priority: int) -> dict:
        return await asyncio.to_thread(self._send_sync, message, title, priority)

    def _send_sync(self, message: str, title: str, priority: int) -> dict:
        body = json.dumps({
            "message": message,
            "title": title,
            "priority": priority
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/api/message",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-YuGoHA-Key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"yuGoHA Server HTTP {exc.code}: {payload}") from exc
