"""Morning market news digest: RSS -> Gemini summary -> Telegram."""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "16"))
MAX_HEADLINES = int(os.getenv("MAX_HEADLINES", "100"))
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODEL", "gemini-flash-latest,gemini-2.5-flash,gemini-2.0-flash"
).split(",") if m.strip()]
UA = {"User-Agent": "Mozilla/5.0 (compatible; MorningBriefBot/1.0)"}
IST = timezone(timedelta(hours=5, minutes=30))

SYSTEM_PROMPT = """You write a concise pre-market news digest for Indian equity markets.
Rules:
- Use ONLY the headlines provided. Do not add facts, prices, targets or numbers that are not in them.
- Do NOT give buy/sell/hold calls, price targets or stop-losses. Report what is in the news and why it may matter.
- If something is unclear or the headlines conflict, say so.
- Plain text only. No markdown symbols like ** or #. Use simple dashes for bullets.
- Keep the whole digest under 3000 characters.
- After each item, add the source name in brackets, e.g. [Moneycontrol].

Format:
MARKET MOOD: 2 lines on overall tone (global cues, SGX/GIFT Nifty, FII/DII if mentioned).
STOCKS IN NEWS: up to 8 items, one line each, with the reason.
SECTORS TO WATCH: up to 4 lines.
EVENTS TODAY: results, IPOs, policy or data releases mentioned.
RISKS: 1-2 lines.
"""


def load_feeds(path="feeds.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["feeds"]


def check_feeds():
    ok = True
    for feed in load_feeds():
        try:
            r = requests.get(feed["url"], headers=UA, timeout=20)
            parsed = feedparser.parse(r.content)
            n = len(parsed.entries)
            status = "OK " if n else "EMPTY"
            if not n:
                ok = False
            print(f"{status} {feed['source']:<14} {n:>3} items  {feed['url']} (HTTP {r.status_code})")
        except Exception as e:
            ok = False
            print(f"FAIL {feed['source']:<14} {feed['url']} -> {e}")
    return ok


def fetch_headlines():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen, items = set(), []
    for feed in load_feeds():
        try:
            r = requests.get(feed["url"], headers=UA, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as e:
            print(f"Skipping {feed['url']}: {e}", file=sys.stderr)
            continue
        for e in parsed.entries:
            title = re.sub(r"\s+", " ", e.get("title", "")).strip()
            if not title:
                continue
            key = re.sub(r"[^a-z0-9]", "", title.lower())[:80]
            if key in seen:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = datetime.fromtimestamp(time.mktime(ts), timezone.utc) if ts else None
            if published and published < cutoff:
                continue
            seen.add(key)
            items.append({
                "source": feed["source"],
                "title": title,
                "link": e.get("link", ""),
                "published": published or datetime.now(timezone.utc),
            })
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:MAX_HEADLINES]


def summarize(items):
    api_key = os.environ["GEMINI_API_KEY"]
    lines = [f"- [{i['source']}] {i['title']}" for i in items]
    today = datetime.now(IST).strftime("%A, %d %B %Y")
    user_text = f"Date: {today}\nHeadlines from the last {LOOKBACK_HOURS} hours:\n\n" + "\n".join(lines)
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
    }
    last_error = None
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=90)
            except requests.RequestException as e:
                last_error = str(e)
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 503):
                last_error = f"{model}: HTTP {r.status_code}"
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 404:
                last_error = f"{model}: model not found"
                break  # try next model name
            if not r.ok:
                last_error = f"{model}: HTTP {r.status_code} {r.text[:300]}"
                break
            data = r.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError):
                last_error = f"{model}: empty response {str(data)[:300]}"
                break
            if text:
                return text
    raise RuntimeError(f"Gemini failed. Last error: {last_error}")


def split_message(text, limit=4000):
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > limit:
            chunks.append(current)
            current = ""
        current += para + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")
        time.sleep(1)


def main():
    if "--check-feeds" in sys.argv:
        sys.exit(0 if check_feeds() else 1)

    items = fetch_headlines()
    if not items:
        send_telegram("No fresh headlines found from the configured feeds this morning.")
        return

    digest = summarize(items)
    today = datetime.now(IST).strftime("%d %b %Y")
    header = f"Morning Market Digest - {today}\n\n"
    top_links = "\n".join(f"- {i['title']}\n  {i['link']}" for i in items[:5])
    footer = (
        "\n\nTop headlines:\n" + top_links +
        "\n\nNews summary only, not investment advice. Verify before acting."
    )
    message = header + digest + footer

    if "--dry-run" in sys.argv:
        print(message)
        return
    send_telegram(message)
    print("Sent.")


if __name__ == "__main__":
    main()
