# Morning Market Digest

Pulls headlines from Moneycontrol, Zee Business and NDTV Profit RSS feeds, summarizes them with Google Gemini (free tier), and sends a pre-market digest to Telegram every weekday at 8:30 AM IST via GitHub Actions.

## Setup

### 1. Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot`, and copy the bot token.
2. Send any message to your new bot (or add it to your channel/group as admin).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id": ...}`. That number is your chat ID (channels start with `-100`).

### 2. Gemini API key (free)
Create one at https://aistudio.google.com (Get API key).

### 3. GitHub
1. Create a new repo and upload all these files, keeping the `.github/workflows/` folder structure.
2. Go to **Settings > Secrets and variables > Actions > New repository secret** and add:
   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Open the **Actions** tab, pick *Morning Market Digest*, click **Run workflow** to test.

### 4. Fix the feed URLs
RSS URLs change. Open each site's RSS page, copy the current feed links into `feeds.json`, then check:

```
pip install -r requirements.txt
python main.py --check-feeds
```

Any line showing EMPTY or FAIL needs a new URL.

## Local testing
```
export GEMINI_API_KEY=...
python main.py --dry-run     # prints the digest, sends nothing
```

## Settings (optional env vars)
- `LOOKBACK_HOURS` (default 16)
- `MAX_HEADLINES` (default 100)
- `GEMINI_MODEL` (comma list, default gemini-flash-latest,gemini-2.5-flash,gemini-2.0-flash)

## Notes
- GitHub cron can run 5-30 minutes late. The 8:30 AM slot leaves room before the 9:15 AM open.
- Scheduled workflows are paused by GitHub after 60 days of no repo activity. Re-enable from the Actions tab or push a small commit.
- Output is a news summary, not advice. If you share it publicly, check SEBI rules on research analyst / investment adviser registration first.
- Use headlines and links only; do not republish full articles.
