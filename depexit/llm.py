"""Gemma 4 client over an OpenAI-compatible Chat Completions endpoint.

Works with:
  * Ollama (local):          DEPEXIT_LLM_BASE_URL=http://localhost:11434/v1   model e.g. gemma4:26b
  * Google AI Studio (hosted): DEPEXIT_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
                               DEPEXIT_LLM_API_KEY=<key>                       model per AI Studio list
  * any vLLM / llama.cpp server exposing /v1/chat/completions

Every call is logged (prompt hash, model, latency, raw response) so AI output in a
capsule can always be traced back to the exact request that produced it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .util import sha256, write_json

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma4:26b"


@dataclass
class LLMConfig:
    base_url: str = field(default_factory=lambda: os.environ.get("DEPEXIT_LLM_BASE_URL", DEFAULT_BASE_URL))
    model: str = field(default_factory=lambda: os.environ.get("DEPEXIT_LLM_MODEL", DEFAULT_MODEL))
    api_key: str | None = field(default_factory=lambda: os.environ.get("DEPEXIT_LLM_API_KEY"))
    temperature: float = 0.1
    timeout: int = field(default_factory=lambda: int(os.environ.get("DEPEXIT_LLM_TIMEOUT", "900")))
    max_tokens: int = 8192
    # Ollama only: its OpenAI-compatible endpoint silently truncates prompts to the
    # server default context (often 4096 tokens), so Ollama is driven via /api/chat.
    num_ctx: int = field(default_factory=lambda: int(os.environ.get("DEPEXIT_LLM_NUM_CTX", "8192")))
    think: bool = False

    def public(self):
        return {"base_url": self.base_url, "model": self.model, "temperature": self.temperature,
                "num_ctx": self.num_ctx}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: LLMConfig, log_dir: Path):
        self.cfg = cfg
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        self._lock = threading.Lock()
        self._ollama: bool | None = None

    def is_ollama(self) -> bool:
        """Detect an Ollama server (GET /api/version) so the native API can be used."""
        if self._ollama is None:
            root = re.sub(r"/v1/?$", "", self.cfg.base_url.rstrip("/"))
            try:
                with urllib.request.urlopen(root + "/api/version", timeout=5) as resp:
                    self._ollama = "version" in json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                self._ollama = False
        return self._ollama

    def _request(self, messages) -> tuple[str, str, dict | None]:
        """One completion. Returns (content, model, usage)."""
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        if self.is_ollama():
            root = re.sub(r"/v1/?$", "", self.cfg.base_url.rstrip("/"))
            url = root + "/api/chat"
            payload = {"model": self.cfg.model, "messages": messages, "stream": False,
                       "think": self.cfg.think,
                       "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx,
                                   "num_predict": min(self.cfg.max_tokens, self.cfg.num_ctx // 2)}}
        else:
            url = self.cfg.base_url.rstrip("/") + "/chat/completions"
            payload = {"model": self.cfg.model, "messages": messages,
                       "temperature": self.cfg.temperature, "max_tokens": self.cfg.max_tokens}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if "message" in body and "choices" not in body:  # Ollama native
            usage = {"prompt_tokens": body.get("prompt_eval_count"),
                     "completion_tokens": body.get("eval_count"), "num_ctx": self.cfg.num_ctx,
                     "done_reason": body.get("done_reason")}
            if (usage["prompt_tokens"] or 0) >= self.cfg.num_ctx - 8:
                raise LLMError(f"prompt filled the whole {self.cfg.num_ctx}-token context window "
                               "and was truncated; raise DEPEXIT_LLM_NUM_CTX")
            return body["message"].get("content") or "", body.get("model", self.cfg.model), usage
        return (body["choices"][0]["message"].get("content") or "", body.get("model", self.cfg.model),
                body.get("usage"))

    def ping(self) -> tuple[bool, str]:
        try:
            self.chat("Reply with the JSON object {\"ok\": true} and nothing else.", "ping",
                      expect_json=True)
            return True, "ok"
        except LLMError as exc:
            return False, str(exc)

    def chat(self, prompt: str, purpose: str, system: str | None = None,
             expect_json: bool = True) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        last_err = None
        for attempt in range(3):
            try:
                content, model, usage = self._request(messages)
                break
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
                if exc.code not in (429, 500, 502, 503, 504):
                    raise LLMError(last_err) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_err = str(exc)
            time.sleep(2 * (attempt + 1))
        else:
            raise LLMError(last_err or "LLM request failed")
        record = {
            "purpose": purpose,
            "model": model,
            "base_url": self.cfg.base_url,
            "api": "ollama-native" if self._ollama else "openai-compatible",
            "prompt_sha256": sha256(prompt),
            "seconds": round(time.monotonic() - started, 2),
            "usage": usage,
            "prompt": prompt,
            "response": content,
        }
        with self._lock:
            self._n += 1
            n = self._n
        safe = re.sub(r"[^\w.-]+", "_", purpose)[:60]
        write_json(self.log_dir / f"{n:03d}_{safe}.json", record)
        meta = {"model": record["model"], "prompt_sha256": record["prompt_sha256"],
                "log": f"llm_calls/{n:03d}_{safe}.json"}
        if not expect_json:
            return {"text": content, "_meta": meta}
        data = extract_json(content)
        if data is None:
            raise LLMError(f"{purpose}: model did not return valid JSON")
        data["_meta"] = meta
        return data


def extract_json(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None
