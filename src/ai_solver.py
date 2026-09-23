"""Optional AI fallback for rapidly changing Microsoft Rewards/Bing markup.

The deterministic Selenium selectors remain the primary path. This module is only
used when the current page variant does not expose the expected selectors. It
supports OpenAI-compatible chat-completions endpoints so the API host and model
can be changed without changing the application.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from src.utils import CONFIG

logger = logging.getLogger(__name__)


class AIAssistant:
    def __init__(self) -> None:
        cfg = CONFIG.get("ai", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.api_key = str(cfg.get("api-key") or cfg.get("api_key") or "").strip()
        self.base_url = str(
            cfg.get("base-url")
            or cfg.get("base_url")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = str(cfg.get("model") or "gpt-5.6-luna").strip()
        self.timeout = max(5, int(cfg.get("timeout", 20)))

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)

    def _chat(self, prompt: str) -> str | None:
        if not self.available:
            return None

        is_openai = "api.openai.com" in self.base_url.lower()
        if is_openai:
            url = f"{self.base_url}/responses"
            payload: dict[str, Any] = {
                "model": self.model,
                "input": prompt,
            }
        else:
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Return only the requested integer. Do not include markdown, "
                            "explanation, or extra text."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            }

        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            if is_openai:
                content = data.get("output_text")
                if not content:
                    pieces = []
                    for item in data.get("output", []):
                        for part in item.get("content", []):
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                pieces.append(str(part.get("text", "")))
                    content = "".join(pieces)
            else:
                content = data["choices"][0]["message"]["content"]

            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            return str(content).strip()
        except Exception as exc:
            logger.warning("[AI] API fallback unavailable: %s", exc)
            return None

    @staticmethod
    def _integer(text: str | None) -> int | None:
        if not text:
            return None
        match = re.search(r"\d+", text)
        return int(match.group(1)) if match else None

    def choose_quiz_option(self, question: str, options: list[str]) -> int | None:
        if not self.available or not options:
            return None
        numbered = "\n".join(f"{i}: {text}" for i, text in enumerate(options))
        prompt = (
            "Choose the objectively correct answer for this quiz question. "
            "Return exactly the zero-based option number. "
            "Question:\\n"
            f"{question[:2000]}\\n\\nOptions:\\n{numbered[:8000]}"
        )
        selected = self._integer(self._chat(prompt))
        return selected if selected is not None and 0 <= selected < len(options) else None

    def choose_interactive_candidate(self, candidates: list[dict[str, str]]) -> int | None:
        if not self.available or not candidates:
            return None
        compact = []
        for i, candidate in enumerate(candidates[:80]):
            compact.append(
                {
                    "index": i,
                    "tag": candidate.get("tag", ""),
                    "text": candidate.get("text", "")[:240],
                    "title": candidate.get("title", "")[:160],
                    "aria": candidate.get("aria", "")[:160],
                    "id": candidate.get("id", "")[:160],
                    "class": candidate.get("class", "")[:320],
                    "href": candidate.get("href", "")[:240],
                }
            )
        prompt = (
            "From these visible Bing page elements, identify the element that is "
            "most likely a Microsoft Rewards quiz or poll answer choice. "
            "Return exactly its integer index. Prefer an option inside a component "
            "whose class/id suggests a quiz or poll; never choose navigation, "
            "search results, sign-in, feedback, or unrelated page controls.\\n"
            f"{json.dumps(compact, ensure_ascii=True)[:18000]}"
        )
        selected = self._integer(self._chat(prompt))
        return selected if selected is not None and 0 <= selected < len(compact) else None
