#!/usr/bin/env python3
"""
KnifeWatch — r/Knife_Swap watcher
Polls Reddit for new posts, matches against a watchlist, sends email alerts,
and harvests pricing data from sold/traded posts.
"""

import json
import os
import re
import smtplib
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests
from price_parser import Price

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDDIT_SUBREDDIT = "Knife_Swap"
REDDIT_JSON_URL = f"https://www.reddit.com/r/{REDDIT_SUBREDDIT}/new.json?limit=50"
REDDIT_RSS_URL = f"https://www.reddit.com/r/{REDDIT_SUBREDDIT}/new/.rss?limit=50"
USER_AGENT = "KnifeWatch/1.0 (by github.com/illtrick/swap-watcher)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
HISTORY_FILE = DATA_DIR / "history.json"
SEEN_FILE = DATA_DIR / "seen_posts.json"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")

SEEN_POST_MAX_AGE_HOURS = 72

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    """Load a JSON file, returning *default* if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    """Write JSON with pretty-print."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Reddit fetching (JSON primary, RSS fallback)
# ---------------------------------------------------------------------------

def fetch_posts_json() -> list[dict] | None:
    """Fetch posts via Reddit's public JSON endpoint. Returns None on failure."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(3):
        try:
            resp = requests.get(REDDIT_JSON_URL, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                posts = []
                for child in children:
                    p = child.get("data", {})
                    posts.append({
                        "id": p.get("name", ""),           # e.g. "t3_abc123"
                        "title": p.get("title", ""),
                        "selftext": p.get("selftext", ""),
                        "url": f"https://www.reddit.com{p.get('permalink', '')}",
                        "author": p.get("author", ""),
                        "created_utc": p.get("created_utc", 0),
                        "flair": (p.get("link_flair_text") or "").lower(),
                    })
                return posts
            elif resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", 30))
                print(f"  Rate limited (429), waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
            else:
                print(f"  JSON endpoint returned {resp.status_code} (attempt {attempt+1})")
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            print(f"  JSON request error: {e} (attempt {attempt+1})")
            time.sleep(2 ** attempt)
    return None


def fetch_posts_rss() -> list[dict] | None:
    """Fallback: fetch posts via RSS feed. Less data but more resilient."""
    try:
        feed = feedparser.parse(REDDIT_RSS_URL)
        if not feed.entries:
            return None
        posts = []
        for entry in feed.entries:
            # RSS doesn't provide flair — we'll check title for [SOLD] hints
            post_id = entry.get("id", entry.get("link", ""))
            # Extract Reddit post ID from the link
            link = entry.get("link", "")
            # RSS entries have content in 'summary' or 'content'
            body = entry.get("summary", "")
            posts.append({
                "id": post_id,
                "title": entry.get("title", ""),
                "selftext": body,
                "url": link,
                "author": entry.get("author", "").replace("/u/", ""),
                "created_utc": time.mktime(entry.get("published_parsed", time.gmtime())),
                "flair": "",  # RSS doesn't include flair
            })
        return posts
    except Exception as e:
        print(f"  RSS fetch error: {e}")
        return None


def fetch_posts() -> list[dict]:
    """Fetch posts with JSON → RSS fallback chain."""
    print("Fetching posts via JSON endpoint...")
    posts = fetch_posts_json()
    if posts is not None:
        print(f"  Got {len(posts)} posts via JSON")
        return posts

    print("JSON failed, trying RSS fallback...")
    posts = fetch_posts_rss()
    if posts is not None:
        print(f"  Got {len(posts)} posts via RSS")
        return posts

    print("Both JSON and RSS failed. Will try again next run.")
    return []


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def matches_watch(text: str, watch: dict) -> bool:
    """
    Check if text matches a watch's required_keywords.
    AND between groups, OR within each group.
    """
    text_lower = text.lower()
    for group in watch.get("required_keywords", []):
        if not any(kw.lower() in text_lower for kw in group):
            return False
    return True


def is_wtb_post(title: str) -> bool:
    """Check if post is a Want-To-Buy (skip these)."""
    return bool(re.search(r"\[WTB\]", title, re.IGNORECASE))


def get_post_status(post: dict) -> str:
    """Determine if post is available or sold/traded."""
    flair = post.get("flair", "")
    title_lower = post.get("title", "").lower()
    if "sold" in flair or "traded" in flair:
        return "sold"
    # Some posts don't get flair but have SOLD in title
    if re.search(r"\bsold\b", title_lower):
        return "sold"
    return "available"


# ---------------------------------------------------------------------------
# Price extraction (Knife_Swap-aware + price-parser)
# ---------------------------------------------------------------------------

def extract_price(title: str, body: str) -> float | None:
    """
    Extract the sale value (SV) price from a Knife_Swap post.
    Uses Knife_Swap conventions first, then falls back to generic price parsing.
    """
    combined = f"{title}\n{body}"

    # Step 1: Look for SV-specific patterns (Sale Value)
    sv_patterns = [
        r"SV[/:]?\s*\$?\s*(\d{2,4}(?:\.\d{2})?)",          # SV: $350, SV $350, SV:350, SV/$350
        r"SV\s*/\s*TV\s*[\$:]?\s*(\d{2,4}(?:\.\d{2})?)",    # SV/TV $350/400 (captures first)
    ]
    for pattern in sv_patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            price = Price.fromstring(f"${match.group(1)}")
            if price.amount is not None:
                return float(price.amount)

    # Step 2: Look for dollar amounts in the title (most reliable location)
    title_price_match = re.search(r"\$\s*(\d{2,4}(?:\.\d{2})?)", title)
    if title_price_match:
        price = Price.fromstring(f"${title_price_match.group(1)}")
        if price.amount is not None:
            return float(price.amount)

    # Step 3: Generic price-parser on the first few lines of body
    # (avoid picking up random numbers from deep in the text)
    first_chunk = combined[:500]
    price_matches = re.findall(r"\$\s*\d{2,4}(?:\.\d{2})?", first_chunk)
    if price_matches:
        price = Price.fromstring(price_matches[0])
        if price.amount is not None:
            return float(price.amount)

    return None


def price_vs_target(price: float | None, target: float | None) -> str:
    """Compare extracted price against target."""
    if price is None or target is None:
        return "unknown"
    if price <= target:
        return "below"
    return "above"


# ---------------------------------------------------------------------------
# Email alerting
# ---------------------------------------------------------------------------

def build_alert_email(matches: list[dict]) -> tuple[str, str, str]:
    """
    Build an HTML email for one or more matches on the SAME post.
    Returns (subject, html_body, text_body).
    """
    post = matches[0]["post"]
    watch_names = [m["watch"]["display_name"] for m in matches]
    subject_name = watch_names[0] if len(watch_names) == 1 else f"{len(watch_names)} watches"

    price = matches[0].get("extracted_price")
    price_str = f"${price:.0f}" if price else "Not found"
    subject_price = f" - {price_str}" if price else ""
    subject = f"🔪 KnifeWatch: {subject_name}{subject_price} on r/Knife_Swap"

    # Build watch detail rows
    watch_rows = ""
    for m in matches:
        w = m["watch"]
        target = w.get("target_price")
        ep = m.get("extracted_price")
        if ep and target:
            diff = target - ep
            if diff >= 0:
                badge = f'<span style="color:#16a34a;font-weight:bold">✅ ${diff:.0f} BELOW TARGET</span>'
            else:
                badge = f'<span style="color:#dc2626;font-weight:bold">⚠️ ${abs(diff):.0f} ABOVE TARGET</span>'
            price_line = f"Asking: <strong>${ep:.0f}</strong> · Target: ${target:.0f} · {badge}"
        elif ep:
            price_line = f"Asking: <strong>${ep:.0f}</strong>"
        else:
            price_line = '<span style="color:#9ca3af">Price not found in post</span>'

        watch_rows += f"""
        <div style="background:#f8f9fa;border-radius:8px;padding:12px 16px;margin-bottom:8px;border-left:4px solid #2563eb">
            <div style="font-weight:bold;font-size:16px;margin-bottom:4px">{w['display_name']}</div>
            <div style="font-size:14px">{price_line}</div>
        </div>
        """

    posted_ago = ""
    created = post.get("created_utc", 0)
    if created:
        mins = (time.time() - created) / 60
        if mins < 60:
            posted_ago = f"{int(mins)} minutes ago"
        else:
            posted_ago = f"{int(mins/60)} hours ago"

    html = f"""
    <div style="font-family:-apple-system,system-ui,sans-serif;max-width:500px;margin:0 auto;color:#1a1a1a">
        <div style="background:#1e293b;color:white;padding:16px 20px;border-radius:12px 12px 0 0">
            <div style="font-size:20px;font-weight:bold">🔪 KnifeWatch Alert</div>
        </div>
        <div style="padding:20px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px">
            {watch_rows}
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid #e2e8f0">
                <div style="font-size:14px;color:#4b5563;margin-bottom:4px">
                    <strong>{post['title'][:120]}</strong>
                </div>
                <div style="font-size:13px;color:#6b7280;margin-bottom:12px">
                    by u/{post['author']} · {posted_ago}
                </div>
                <a href="{post['url']}" style="display:inline-block;background:#ff4500;color:white;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:14px">
                    View on Reddit →
                </a>
            </div>
        </div>
    </div>
    """

    text = f"""KnifeWatch Alert: {subject_name}

{chr(10).join(w['display_name'] for w in [m['watch'] for m in matches])}

Post: {post['title']}
Link: {post['url']}
Price: {price_str}
Posted: {posted_ago}
By: u/{post['author']}
"""

    return subject, html, text


def send_email(subject: str, html_body: str, text_body: str):
    """Send an alert email via Gmail SMTP."""
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL]):
        print("  ⚠ Email credentials not configured, skipping email send")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"KnifeWatch <{GMAIL_ADDRESS}>"
    msg["To"] = ALERT_EMAIL

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, ALERT_EMAIL, msg.as_string())
        print(f"  ✉ Email sent: {subject}")
        return True
    except Exception as e:
        print(f"  ✉ Email failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    print(f"=== KnifeWatch run at {datetime.now(timezone.utc).isoformat()} ===")

    # Load data
    watchlist = load_json(WATCHLIST_FILE, [])
    history = load_json(HISTORY_FILE, [])
    seen = load_json(SEEN_FILE, {"last_updated": "", "posts": {}})

    active_watches = [w for w in watchlist if w.get("active", True)]
    if not active_watches:
        print("No active watches. Nothing to do.")
        save_seen_and_exit(seen)
        return

    print(f"Active watches: {len(active_watches)}")

    # Fetch posts
    posts = fetch_posts()
    if not posts:
        print("No posts fetched. Exiting.")
        save_seen_and_exit(seen)
        return

    # Process posts
    new_history_entries = []
    watchlist_modified = False
    posts_to_email: dict[str, list[dict]] = {}  # post_id -> list of match dicts

    for post in posts:
        post_id = post["id"]

        # Skip already-seen posts
        if post_id in seen.get("posts", {}):
            continue

        # Mark as seen
        seen.setdefault("posts", {})[post_id] = int(time.time())

        # Skip WTB posts
        if is_wtb_post(post["title"]):
            continue

        status = get_post_status(post)
        search_text = f"{post['title']} {post['selftext']}"

        for watch in active_watches:
            if not matches_watch(search_text, watch):
                continue

            # Match found!
            price = extract_price(post["title"], post["selftext"])
            pvt = price_vs_target(price, watch.get("target_price"))

            print(f"  🎯 Match: '{watch['display_name']}' → {post['title'][:80]}")
            print(f"     Status: {status} | Price: ${price:.0f if price else 'N/A'} | vs target: {pvt}")

            # Build history entry
            entry = {
                "id": f"h_{uuid.uuid4().hex[:6]}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "post_id": post_id,
                "post_title": post["title"],
                "post_url": post["url"],
                "post_author": post.get("author", ""),
                "post_status": status,
                "matched_watch_id": watch["id"],
                "matched_watch_name": watch["display_name"],
                "extracted_price": price,
                "target_price": watch.get("target_price"),
                "price_vs_target": pvt,
                "email_sent": False,  # updated below if applicable
            }

            # Append price data to watch's price_history
            if price is not None:
                source = "watcher_sold" if status == "sold" else "watcher"
                label = "Confirmed sale" if status == "sold" else "Active listing"
                price_point = {
                    "price": price,
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "source": source,
                    "post_id": post_id,
                    "label": label,
                }
                watch.setdefault("price_history", []).append(price_point)
                watchlist_modified = True

            # Queue email for available posts only
            if status == "available":
                match_data = {"post": post, "watch": watch, "extracted_price": price}
                posts_to_email.setdefault(post_id, []).append(match_data)
                entry["email_sent"] = True  # optimistic, corrected below if send fails

            new_history_entries.append(entry)

    # Send emails (one per post, listing all matched watches)
    for post_id, match_list in posts_to_email.items():
        subject, html, text = build_alert_email(match_list)
        success = send_email(subject, html, text)
        if not success:
            # Mark entries as email not sent
            for entry in new_history_entries:
                if entry["post_id"] == post_id:
                    entry["email_sent"] = False

    # Prune old seen posts
    cutoff = int(time.time()) - (SEEN_POST_MAX_AGE_HOURS * 3600)
    seen["posts"] = {
        pid: ts for pid, ts in seen.get("posts", {}).items()
        if ts > cutoff
    }
    seen["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Save everything
    if new_history_entries:
        history = new_history_entries + history  # newest first
        save_json(HISTORY_FILE, history)
        print(f"Added {len(new_history_entries)} history entries")

    if watchlist_modified:
        save_json(WATCHLIST_FILE, watchlist)
        print("Updated watchlist price_history")

    save_json(SEEN_FILE, seen)
    print(f"Seen posts: {len(seen['posts'])} (pruned to {SEEN_POST_MAX_AGE_HOURS}h)")
    print("=== Done ===")


def save_seen_and_exit(seen):
    """Save seen posts and exit cleanly."""
    seen["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_json(SEEN_FILE, seen)


if __name__ == "__main__":
    run()
