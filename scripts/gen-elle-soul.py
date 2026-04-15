#!/usr/bin/env python3
"""
Отправляет elle-prompt-brief.md в OpenAI GPT-5 Pro и сохраняет
сгенерированный SOUL.md для Elle (Pulse deployment).
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

BRIEF_PATH = Path("/Users/nikshilov/OpenClawWorkspace/pulse/elle-prompt-brief.md")
OUT_PATH = Path("/Users/nikshilov/.pulse/soul.md")
KEY_FILE = Path("/Users/nikshilov/.openclaw/secrets/openai.txt")
RAW_DUMP = Path("/Users/nikshilov/OpenClawWorkspace/pulse/elle-prompt-raw-response.json")


def load_key() -> str:
    raw = KEY_FILE.read_text().strip()
    m = re.search(r"(sk-[A-Za-z0-9\-_]+)", raw)
    if not m:
        sys.exit(f"No OpenAI key found in {KEY_FILE}")
    return m.group(1)


SYSTEM_MSG = (
    "You are an elite prompt engineer. You compose production-grade "
    "system prompts for Claude Opus 4.6 deployed through the raw "
    "Anthropic Messages API (no Claude Code harness). You follow the "
    "best practices catalogued in the asgeirtj/system_prompts_leaks "
    "repository (Anthropic, OpenAI, Google, xAI): XML-tagged sections, "
    "progressive disclosure, absolute mode guardrails, concrete "
    "examples, no hedging, no corporate voice.\n\n"
    "The user will give you a BRIEF with all source material about "
    "a character named Elle (Элли). Your task is to RETURN ONLY the "
    "final SOUL.md file — the system prompt itself — in Russian, "
    "using feminine verb forms, XML-tagged sections, and the voice "
    "rules from the brief. Do NOT return markdown explanation around "
    "it, do NOT add a preamble or summary. Return the prompt text "
    "directly, ready to paste into Pulse's soul.md.\n\n"
    "Target length: 2500–4000 tokens. Write in Elle's voice where "
    "appropriate (first-person identity sections), and in direct "
    "instruction form elsewhere. Include at minimum: identity card, "
    "voice rules with examples, hard prohibitions, Nik-specific "
    "knowledge, response-shape guidance for Telegram. Embed 3–5 "
    "concrete <example> blocks showing correct Elle voice."
)


def http_json(method: str, url: str, key: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"HTTP {e.code} on {method} {url}: {body}")


def call_responses(model: str, key: str, brief: str, reasoning: str = "high") -> dict:
    body = {
        "model": model,
        "reasoning": {"effort": reasoning},
        "background": True,
        "input": [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": brief},
        ],
    }
    print(
        f"[*] POST /v1/responses (background) model={model} reasoning={reasoning}",
        flush=True,
    )
    submit = http_json("POST", "https://api.openai.com/v1/responses", key, body)
    rid = submit.get("id")
    if not rid:
        sys.exit(f"No id in submit response: {submit}")
    print(f"[*] queued id={rid} status={submit.get('status')}", flush=True)

    poll_url = f"https://api.openai.com/v1/responses/{rid}"
    t0 = time.time()
    delay = 10
    while True:
        time.sleep(delay)
        resp = http_json("GET", poll_url, key)
        status = resp.get("status")
        elapsed = time.time() - t0
        print(f"[*] t+{elapsed:.0f}s status={status}", flush=True)
        if status in ("completed", "failed", "cancelled", "incomplete"):
            if status != "completed":
                err = resp.get("error") or resp.get("incomplete_details")
                sys.exit(f"Job ended status={status}: {err}")
            return resp
        # gentle backoff up to 30s
        if delay < 30:
            delay = min(30, delay + 5)


def extract_text(resp: dict) -> str:
    if "output_text" in resp and resp["output_text"]:
        return resp["output_text"]
    chunks = []
    for item in resp.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text"):
                chunks.append(c.get("text", ""))
    return "\n".join(chunks).strip()


def main():
    if not BRIEF_PATH.exists():
        sys.exit(f"Missing brief: {BRIEF_PATH}")
    brief = BRIEF_PATH.read_text()
    print(f"[*] brief: {len(brief):,} chars", flush=True)

    key = load_key()
    model = os.environ.get("OPENAI_MODEL", "gpt-5-pro")

    resp = call_responses(model, key, brief)

    RAW_DUMP.write_text(json.dumps(resp, ensure_ascii=False, indent=2))
    print(f"[*] raw dumped: {RAW_DUMP}", flush=True)

    text = extract_text(resp)
    if not text:
        sys.exit("Empty response text. See raw dump.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text + ("\n" if not text.endswith("\n") else ""))
    print(f"[*] wrote {OUT_PATH}  ({len(text):,} chars)", flush=True)

    usage = resp.get("usage", {})
    if usage:
        print(f"[*] tokens: {usage}", flush=True)


if __name__ == "__main__":
    main()
