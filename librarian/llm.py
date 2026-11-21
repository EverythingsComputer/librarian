"""LLM helpers: extractive fallback + optional OpenAI-compatible chat for doc cards."""
from __future__ import annotations

import re


def extractive_summary(text: str, max_chars: int = 350) -> str:
    """First meaningful paragraphs, trimmed to sentence boundaries."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    body = []
    for l in lines:
        if re.match(r"^[\W_]+$", l):
            continue
        body.append(l)
        if sum(len(x) for x in body) > max_chars + 120:
            break
    s = " ".join(body)
    s = re.sub(r"\s+", " ", s)[:max_chars + 120]
    m = list(re.finditer(r"[.!?](\s|$)", s))
    if m and len(m[-1].group(0)) and m[-1].end() > max_chars * 0.5:
        s = s[:m[-1].end()]
    return s.strip()


class ChatLLM:
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, system: str, user: str, max_tokens: int = 300, temperature: float = 0.2) -> str:
        import httpx
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            headers={"Authorization": f"Bearer {self.api_key or 'none'}"},
            timeout=300,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or ""
        # strip thinking blocks some local models emit
        content = re.sub(r"<\|?[Tt]hinking\|?>.*?<\|?/[Tt]hinking\|?>", "", content, flags=re.S)
        return content.strip()

    def summarize_doc(self, title: str, full_text: str) -> str:
        sample = _sample(full_text, 5000)
        out = self.chat(
            "You write catalog cards for a document library. Reply with 2-3 sentences: "
            "what kind of document this is, what topics it covers, and any specifics "
            "(dates, orgs, standards, tool names) that help routing. No preamble.",
            f"FILE: {title}\n\nTEXT:\n{sample}",
            max_tokens=200,
        )
        return out[:600]


def _sample(text: str, budget: int) -> str:
    head = text[: budget - 400]
    tail = text[-(budget - (len(head) + 30)):] if len(text) > budget else ""
    return head + ("\n…\n" + tail if tail else "")
