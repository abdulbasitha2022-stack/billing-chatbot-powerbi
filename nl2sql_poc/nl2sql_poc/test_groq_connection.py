#!/usr/bin/env python3
"""Minimal Groq API connectivity test. Reads GROQ_API_KEY from the environment."""
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
if not API_KEY:
    sys.exit("GROQ_API_KEY is not set. Set it in this PowerShell window and try again.")

url = "https://api.groq.com/openai/v1/chat/completions"
payload = {
    "model": "openai/gpt-oss-120b",
    "messages": [
        {"role": "system", "content": "You are testing API connectivity. Reply with exactly: SELECT 1 AS groq_connection_test"},
        {"role": "user", "content": "Return the test SQL only. No markdown."},
    ],
}
req = Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "billing-poc-connectivity-test/1.0",
    },
    method="POST",
)

try:
    with urlopen(req, timeout=45) as response:
        result = json.loads(response.read().decode("utf-8"))
except HTTPError as exc:
    details = exc.read().decode("utf-8", errors="replace")
    sys.exit(f"Groq API returned HTTP {exc.code}. Details: {details[:1200]}")
except URLError as exc:
    sys.exit(f"Could not connect to Groq API: {exc.reason}")
except TimeoutError:
    sys.exit("Groq API request timed out. Try again in a moment.")

try:
    answer = result["choices"][0]["message"]["content"]
except (KeyError, IndexError, TypeError):
    sys.exit("Groq returned an unexpected response: " + json.dumps(result)[:1200])

usage = result.get("usage") or {}
print("Groq API connection successful.")
print("Model:", result.get("model", "openai/gpt-oss-120b"))
print("Model response:", answer)
print("Input tokens:", usage.get("prompt_tokens", "not reported"))
print("Output tokens:", usage.get("completion_tokens", "not reported"))
