# 🔪 KnifeWatch — r/Knife_Swap Alert System

Monitors [r/Knife_Swap](https://www.reddit.com/r/Knife_Swap/) for knives you're hunting and emails you when they appear. Silently harvests confirmed sale prices to build pricing intelligence over time.

## How it works

- **GitHub Action** polls r/Knife_Swap every 15 minutes (8am–midnight PT)
- Matches new posts against your watchlist using keyword groups
- **Available posts** → sends an HTML email alert with price comparison
- **Sold/Traded posts** → no email, but captures the confirmed sale price
- All data stored as JSON in this repo — version-controlled and inspectable

## Quick setup

### 1. Set repository secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `GMAIL_ADDRESS` | Your Gmail address to send from |
| `GMAIL_APP_PASSWORD` | [Gmail app password](https://myaccount.google.com/apppasswords) (16 chars) |
| `ALERT_EMAIL` | Email to receive alerts (can be same as sender) |

### 2. Add a watch

Edit `data/watchlist.json` and add an entry:

```json
[
  {
    "id": "w_bugout535",
    "make": "Benchmade",
    "model": "Bugout 535",
    "display_name": "Benchmade Bugout 535",
    "required_keywords": [
      ["benchmade", "bm"],
      ["bugout", "535"]
    ],
    "target_price": 100,
    "msrp": 155,
    "notes": "Prefer lightweight, any color",
    "active": true,
    "created_at": "2026-04-13T00:00:00Z",
    "price_history": []
  }
]
```

**Keyword logic:** AND between groups, OR within each group.  
The example matches posts containing ("benchmade" OR "bm") AND ("bugout" OR "535").

### 3. Test it

Go to **Actions → KnifeWatch → Run workflow** to trigger a manual run.  
Check the action logs to see matches and email delivery.

### 4. It runs automatically

The cron schedule activates on push. Every 15 minutes during active hours, KnifeWatch checks for new posts.

## Data files

| File | Purpose |
|------|---------|
| `data/watchlist.json` | Your watches with keyword groups, target prices, and accumulated price history |
| `data/history.json` | Every matched post (available + sold) with timestamps and prices |
| `data/seen_posts.json` | Dedup tracker — pruned to last 72 hours |

## Keyword tips

- Include common abbreviations and misspellings: `["hinderer", "hindy"]`
- Include hyphenated and unhyphenated variants: `["xm-18", "xm18", "xm 18"]`
- More keyword groups = more specific matching (fewer false positives)
- Fewer keyword groups = broader matching (fewer missed posts)

## Price extraction

The watcher understands r/Knife_Swap pricing conventions:
- `SV: $350` / `SV $350` / `SV/TV $350/400` → extracts sale value
- `$350` in title → direct dollar amount
- Uses the [price-parser](https://github.com/scrapinghub/price-parser) library for robust number parsing

## Architecture

```
GitHub Actions (cron) ──→ Reddit JSON/RSS ──→ Match ──→ Email alert
         │                                       │
         └── commit data ←── price_history ──────┘
```

Dashboard (Phase 2) will be a React artifact in Claude.ai that reads this repo's data files directly.

## Troubleshooting

**Workflow not running?** GitHub disables cron workflows after 60 days of no repo activity. Since the watcher auto-commits on every run, this shouldn't happen. If it does, trigger a manual run from the Actions tab.

**No email?** Check that your Gmail app password is correct and that your Google account allows "less secure apps" or has an app password configured. Check the Action logs for error details.

**Reddit blocking?** The watcher falls back to RSS if the JSON endpoint returns 403/429. Check Action logs for `"RSS fallback"` messages. At 1 request per 15 minutes, rate limiting should be extremely rare.
