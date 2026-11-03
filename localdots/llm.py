"""OpenAI 互換サーバー (/v1/chat/completions) の最小クライアント。標準ライブラリのみ。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import BackendConfig

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(Exception):
    pass


class ChatModel(Protocol):
    """agent が使うのはこれだけ。テストでは台本どおりに返す偽物に差し替える。"""

    def chat(self, messages: list[dict[str, str]], *, temperature: float | None = None) -> str: ...


class ChatClient:
    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg
        self.model = cfg.model

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        url = self.cfg.base_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.cfg.request_timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"{method} {url}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"{method} {url}: {e}") from e

    def list_models(self, timeout: float = 5) -> list[str]:
        return [m["id"] for m in self._request("GET", "/models", timeout=timeout).get("data", [])]

    def chat(self, messages: list[dict[str, str]], *, temperature: float | None = None) -> str:
        if not self.model:
            models = self.list_models()
            if not models:
                raise LLMError("サーバーにモデルが読み込まれていません")
            self.model = models[0]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.cfg.thinking},
        }
        res = self._request("POST", "/chat/completions", body)
        try:
            content = res["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"応答の形式が想定外です: {str(res)[:300]}") from e
        return THINK_RE.sub("", content).strip()
