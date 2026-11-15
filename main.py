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
GEMINI_MODELS_ENV = [m.strip() for m in os.getenv("GEMINI_MODEL", "").split(",") if m.strip()]
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
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


def fetch_headlines(report=None):
    report = report if report is not None else []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen, items = set(), []
    for feed in load_feeds():
        label = f"{feed['source']} ({'GoogleNews' if 'news.google.com' in feed['url'] else 'direct'})"
        try:
            r = requests.get(feed["url"], headers=UA, timeout=25)
            if r.status_code != 200:
                report.append(f"{label}: HTTP {r.status_code}")
                print(report[-1], file=sys.stderr)
                continue
            parsed = feedparser.parse(r.content)
        except Exception as e:
            report.append(f"{label}: error {type(e).__name__}")
            print(f"Skipping {feed['url']}: {e}", file=sys.stderr)
            continue
        total, kept = len(parsed.entries), 0
        for e in parsed.entries:
            title = re.sub(r"\s+", " ", e.get("title", "")).strip()
            if "news.google.com" in feed["url"]:
                title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
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
            kept += 1
            items.append({
                "source": feed["source"],
                "title": title,
                "link": e.get("link", ""),
                "published": published or datetime.now(timezone.utc),
            })
        report.append(f"{label}: {total} found, {kept} fresh")
        print(report[-1])
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:MAX_HEADLINES]


def discover_models(api_key):
    """Ask Google which Flash models this key can use right now."""
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
            headers={"x-goog-api-key": api_key}, timeout=30)
        if not r.ok:
            print(f"Model list failed: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
            return []
        names = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name:
                continue
            if any(bad in name for bad in ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "preview-0", "exp")):
                continue
            names.append(name)
        # prefer "latest" aliases, then newest-looking names
        names.sort(key=lambda n: (0 if "latest" in n else 1, "lite" in n, n), reverse=False)
        return names
    except Exception as e:
        print(f"Model list error: {e}", file=sys.stderr)
        return []


def summarize(items):
    api_key = os.environ["GEMINI_API_KEY"].strip()
    lines = [f"- [{i['source']}] {i['title']}" for i in items]
    today = datetime.now(IST).strftime("%A, %d %B %Y")
    user_text = f"Date: {today}\nHeadlines from the last {LOOKBACK_HOURS} hours:\n\n" + "\n".join(lines)
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
    }
    models = GEMINI_MODELS_ENV or []
    for m in discover_models(api_key)[:6] + FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    print("Models to try:", models)

    errors = []
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=90)
            except requests.RequestException as e:
                errors.append(f"{model}: {type(e).__name__}")
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 503) and attempt == 0:
                errors.append(f"{model}: HTTP {r.status_code} (retrying) {r.text[:150]}")
                time.sleep(20)
                continue
            if not r.ok:
                errors.append(f"{model}: HTTP {r.status_code} {r.text[:200]}")
                break
            data = r.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError):
                errors.append(f"{model}: empty response {str(data)[:200]}")
                break
            if text:
                print(f"Used model: {model}")
                return text
            errors.append(f"{model}: blank text")
            break
    raise RuntimeError("Gemini failed. All errors:\n" + "\n".join(errors))


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

    report = []
    items = fetch_headlines(report)
    if not items:
        send_telegram("No fresh headlines found. Feed status:\n" + "\n".join(report))
        return

    try:
        digest = summarize(items)
    except Exception as e:
        send_telegram("Digest failed at the AI step:\n" + str(e)[:3500])
        raise
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
