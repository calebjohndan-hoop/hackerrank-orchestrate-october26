"""Optional vision-model transcription of screenshot evidence (Groq, OpenAI-compatible API).

Local OCR (RapidOCR) always runs. When GROQ_API_KEY is set, the screenshot is also sent to a
Groq vision model with the prompt in code/prompts/image_extraction.md, and its transcription is
appended to the OCR text, so enabling the model can only add evidence, never remove it.
Results are cached by image+prompt hash under code/cache/vision/ (re-runs cost nothing) and
each real call is recorded for the usage report. Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from .common import REPO_ROOT

PROMPT_FILE = REPO_ROOT / "code" / "prompts" / "image_extraction.md"
CACHE_DIR = REPO_ROOT / "code" / "cache" / "vision"
BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# Model ids get retired; the first one the account can use wins.
MODELS = [m for m in [os.environ.get("GROQ_VISION_MODEL", "")] if m] + [
    "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b"]
# USD per 1M tokens (input, output); list-price assumptions for the usage report.
PRICES = {"meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34), "qwen/qwen3.8-27b": (0.80, 4.00),
          "qwen/qwen3.6-27b": (0.80, 4.00)}

CALLS: list[dict] = []          # real (uncached) calls made in this process
CACHE_HITS = 0
_bad_models: set[str] = set()
_disabled = False               # circuit breaker after auth or network failure


def enabled() -> bool:
    return bool(os.environ.get("GROQ_API_KEY")) and not _disabled


def transcribe(path) -> str:
    """Vision transcription of one screenshot, or '' when disabled or failing."""
    global _disabled, CACHE_HITS
    if not enabled():
        return ""
    data = path.read_bytes()
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    key = hashlib.sha256(prompt.encode() + data).hexdigest()
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        CACHE_HITS += 1
        return json.loads(cache.read_text(encoding="utf-8"))["text"]
    url = "data:image/png;base64," + base64.b64encode(data).decode()
    content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]
    for model in MODELS:
        if model in _bad_models:
            continue
        body = json.dumps({"model": model, "temperature": 0, "max_tokens": 1500,
                           "messages": [{"role": "user", "content": content}]}).encode()
        req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                                              "Content-Type": "application/json",
                                              "User-Agent": "ghost-data-audit/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    out = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:200].lower()
                if e.code == 429:
                    time.sleep(float(e.headers.get("retry-after") or 3 * 2 ** attempt))
                    continue
                if e.code in (400, 404) and ("model" in msg or "decommission" in msg):
                    _bad_models.add(model)
                    break
                if e.code in (401, 403):
                    print(f"[vision] Groq rejected the API key ({e.code}); continuing with OCR only")
                    _disabled = True
                    return ""
                time.sleep(2 ** attempt)
                continue
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"[vision] Groq unreachable ({e}); continuing with OCR only")
                _disabled = True
                return ""
            text = (out["choices"][0]["message"]["content"] or "").strip()
            usage = out.get("usage", {})
            CALLS.append({"model": model, "input_tokens": int(usage.get("prompt_tokens") or 0),
                          "output_tokens": int(usage.get("completion_tokens") or 0)})
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"model": model, "text": text}, ensure_ascii=False), encoding="utf-8")
            return text
    print(f"[vision] no Groq vision model accepted {path.name}; continuing with OCR only")
    return ""
