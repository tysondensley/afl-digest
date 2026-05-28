#!/usr/bin/env python3
"""AFL news digest — fetches RSS, forum threads, and web search, then emails a summary."""

import os
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
from bs4 import BeautifulSoup
import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AEST = timezone(timedelta(hours=10))

RSS_FEEDS = [
    ("AFL.com.au",   "https://www.afl.com.au/rss"),
    ("Fox Footy",    "https://www.foxsports.com.au/feed/sport/afl"),
    ("The Age AFL",  "https://www.theage.com.au/rss/sport/afl.xml"),
    ("Herald Sun",   "https://www.heraldsun.com.au/heraldsun/feeds/rss/sport/afl"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AFL-Digest/1.0; "
        "+https://github.com/tysondensley/afl-digest)"
    )
}

REDDIT_HEADERS = {
    "User-Agent": "AFL-Digest/1.0 (personal newsletter; by /u/afl_digest_bot)"
}

# Patterns that indicate match-report / results content — excluded
_EXCLUDE_RE = re.compile(
    r"\b("
    r"score[sd]?|full[\s\-]?time|half[\s\-]?time|quarter[s]?|"
    r"goal[s]?|behind[s]?|defeated|beat\b|won\b|loss|lost\b|"
    r"highlights?|replay|live blog|as it happened|match report|match preview|"
    r"round \d+ results?"
    r")\b",
    re.IGNORECASE,
)

# Patterns that suggest genuine news — used to boost filtering priority
_INCLUDE_RE = re.compile(
    r"\b("
    r"trade[sd]?|trading|injur(y|ies|ed)|sign(ing|ed|s)?|contract|"
    r"suspension|suspended|deregistered|delisted|draft|recruit|"
    r"breaking|exclusive|interview|investigation|reveals?|confirms?"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT = "Tyson.Densley@afl.com.au"
MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Time-slot helpers
# ---------------------------------------------------------------------------

def get_time_slot() -> str:
    """Return the human label for this run's time slot."""
    override = os.environ.get("TIME_SLOT")
    if override:
        return override
    hour = datetime.now(AEST).hour
    if hour < 10:
        return "6am"
    if hour < 15:
        return "Midday"
    return "End of Day"


# ---------------------------------------------------------------------------
# Section 1 — RSS news
# ---------------------------------------------------------------------------

def fetch_rss_articles() -> list[dict]:
    articles = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            for entry in feed.entries[:25]:
                articles.append({
                    "source": source,
                    "title": entry.get("title", "").strip(),
                    "snippet": re.sub(r"<[^>]+>", "", entry.get("summary", entry.get("description", ""))[:400]).strip(),
                    "link": entry.get("link", ""),
                })
        except Exception as exc:
            print(f"  RSS warning [{source}]: {exc}")
    return articles


def filter_articles(articles: list[dict]) -> list[dict]:
    """Drop obvious match-result content; prefer genuine-news articles."""
    kept, deprioritised = [], []
    for a in articles:
        text = f"{a['title']} {a['snippet']}"
        if _EXCLUDE_RE.search(text):
            continue
        if _INCLUDE_RE.search(text):
            kept.append(a)
        else:
            deprioritised.append(a)
    # Return genuine-news first, then the rest, deduplicated by title
    seen: set[str] = set()
    result = []
    for a in kept + deprioritised:
        key = a["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            result.append(a)
    return result


def summarise_news(articles: list[dict], client: anthropic.Anthropic) -> str:
    if not articles:
        return "<p>No relevant news found in this period.</p>"

    lines = [
        f"[{a['source']}] {a['title']}: {a['snippet']}"
        for a in articles[:30]
    ]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=(
            "You are a senior AFL editor producing a concise news briefing. "
            "Summarise the following AFL news items into EXACTLY 7 bullet points. "
            "Priorities: trades, injuries, signings, contracts, suspensions, genuine breaking news. "
            "Exclude match scores, results, and club PR/promotional copy. "
            "Favour independent journalism over club-issued statements. "
            "Keep each bullet to one clear sentence. "
            "Return ONLY a valid HTML <ul> element with exactly 7 <li> items. No surrounding text."
        ),
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Section 2 — Forum buzz
# ---------------------------------------------------------------------------

def fetch_reddit_threads() -> list[dict]:
    url = "https://www.reddit.com/r/AFL/hot.json?limit=10"
    try:
        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        posts = resp.json()["data"]["children"]
        threads = []
        for post in posts[:5]:
            p = post["data"]
            threads.append({
                "title": p.get("title", ""),
                "comments": p.get("num_comments", 0),
                "score": p.get("score", 0),
                "flair": p.get("link_flair_text", ""),
            })
        return threads
    except Exception as exc:
        print(f"  Reddit warning: {exc}")
        return []


def fetch_bigfooty_threads() -> list[dict]:
    """Scrape BigFooty forum index. Fails silently if blocked or unavailable."""
    try:
        resp = requests.get(
            "https://www.bigfooty.com/forum/",
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        threads = []
        # Try common forum thread selectors
        for sel in ("a.structItem-title", "h3.structItem-title a", "a[href*='/threads/']"):
            for tag in soup.select(sel)[:10]:
                title = tag.get_text(strip=True)
                if title and len(title) > 15:
                    threads.append({"title": title, "url": tag.get("href", "")})
            if threads:
                break
        return threads[:5]
    except Exception:
        return []


def summarise_forum_buzz(
    reddit: list[dict],
    bigfooty: list[dict],
    client: anthropic.Anthropic,
) -> str:
    if not reddit and not bigfooty:
        return "<p>No forum data available this period.</p>"

    lines = []
    if reddit:
        lines.append("Reddit r/AFL — hottest threads:")
        for t in reddit:
            flair = f" [{t['flair']}]" if t["flair"] else ""
            lines.append(f"  • {t['title']}{flair} — {t['comments']} comments, score {t['score']}")
    if bigfooty:
        lines.append("\nBigFooty — trending threads:")
        for t in bigfooty:
            lines.append(f"  • {t['title']}")

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=(
            "You are an AFL analyst watching fan discussion. "
            "Summarise these forum threads into 3–5 bullet points. "
            "For each bullet: name the topic and note the general fan sentiment (excited, frustrated, divided, etc.). "
            "Return ONLY a valid HTML <ul> element with 3–5 <li> items. No surrounding text."
        ),
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Section 3 — Web catch-all
# ---------------------------------------------------------------------------

def fetch_web_catchall(client: anthropic.Anthropic) -> str | None:
    """
    Ask Claude to search the web for significant AFL news from the last 6 hours
    that mainstream RSS feeds are unlikely to cover. Returns HTML bullet list or
    None if nothing novel was found.
    """
    system = (
        "You are an AFL news scout. Search for significant AFL news or stories "
        "from the last 6 hours that are unlikely to appear in mainstream AFL RSS feeds — "
        "breaking stories, controversies, viral content, player social media incidents, "
        "or under-reported news. "
        "If you find nothing genuinely novel or significant, output only the single word: NOTHING. "
        "Otherwise output ONLY a valid HTML <ul> with 3–5 <li> bullet points, no surrounding text."
    )
    user_prompt = (
        "Search for any significant AFL news from the past 6 hours not covered by "
        "AFL.com.au, Fox Footy, The Age, or Herald Sun RSS feeds. "
        "Think: breaking controversies, viral moments, social media stories, "
        "or anything the mainstream AFL media is slow on. "
        "If nothing novel, reply NOTHING."
    )

    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    try:
        for _ in range(6):  # max agentic iterations
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                system=system,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        text = block.text.strip()
                        if not text or text.upper() == "NOTHING":
                            return None
                        return text
                return None

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "",
                    }
                    for block in response.content
                    if block.type == "tool_use"
                ]
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                continue

            break  # unexpected stop reason

    except Exception as exc:
        print(f"  Web catch-all warning: {exc}")

    return None


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

def build_email_html(
    news_html: str,
    forum_html: str,
    web_html: str | None,
    time_slot: str,
    now_aest: datetime,
) -> str:
    day_date = now_aest.strftime("%A %-d %B %Y")   # e.g. Thursday 28 May 2026
    generated = now_aest.strftime("%-I:%M %p AEST")

    ul_style = (
        "margin: 0; padding: 0 0 0 20px; font-size: 14px; "
        "line-height: 1.7; color: #333333;"
    )
    li_style = "margin-bottom: 6px;"

    def wrap_list(raw: str) -> str:
        """Inject inline styles onto <ul>/<li> so Gmail renders them correctly."""
        raw = re.sub(r"<ul([^>]*)>", f'<ul style="{ul_style}">', raw)
        raw = re.sub(r"<li([^>]*)>", f'<li style="{li_style}">', raw)
        return raw

    web_section = ""
    if web_html:
        web_section = f"""
          <!-- Section 3 -->
          <tr>
            <td style="padding: 20px 0 8px 0; border-top: 1px solid #eeeeee;">
              <h2 style="margin: 0 0 12px 0; font-size: 16px; font-weight: bold;
                         color: #003087; text-transform: uppercase; letter-spacing: 0.05em;">
                Breaking / Viral
              </h2>
              {wrap_list(web_html)}
            </td>
          </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AFL Digest &mdash; {time_slot}</title>
</head>
<body style="margin:0;padding:0;background-color:#f0f2f5;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
         style="background-color:#f0f2f5;">
    <tr>
      <td align="center" style="padding:24px 12px;">

        <table role="presentation" cellspacing="0" cellpadding="0" border="0"
               width="100%" style="max-width:600px;background-color:#ffffff;
               border-radius:10px;overflow:hidden;
               box-shadow:0 2px 12px rgba(0,0,0,0.10);">

          <!-- ── Header ── -->
          <tr>
            <td style="background-color:#003087;padding:24px 28px 20px 28px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td>
                    <p style="margin:0;font-size:11px;font-weight:bold;color:#7faad4;
                               text-transform:uppercase;letter-spacing:0.1em;">AFL Digest</p>
                    <h1 style="margin:4px 0 0 0;font-size:26px;font-weight:bold;color:#ffffff;">
                      {time_slot}
                    </h1>
                    <p style="margin:4px 0 0 0;font-size:13px;color:#a8c4e0;">{day_date}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- ── Body ── -->
          <tr>
            <td style="padding:0 28px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">

                <!-- Section 1: News -->
                <tr>
                  <td style="padding:22px 0 8px 0;">
                    <h2 style="margin:0 0 12px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      News &amp; Transfers
                    </h2>
                    {wrap_list(news_html)}
                  </td>
                </tr>

                <!-- Section 2: Forum Buzz -->
                <tr>
                  <td style="padding:20px 0 8px 0;border-top:1px solid #eeeeee;">
                    <h2 style="margin:0 0 12px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      Fan Buzz
                    </h2>
                    {wrap_list(forum_html)}
                  </td>
                </tr>

                {web_section}

              </table>
            </td>
          </tr>

          <!-- ── Footer ── -->
          <tr>
            <td style="background-color:#f8f9fb;padding:14px 28px;
                       border-top:1px solid #e8eaed;">
              <p style="margin:0;font-size:11px;color:#999999;">
                Automated AFL Digest &middot; Generated {generated}
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(html_body: str, subject: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AFL Digest <{gmail_user}>"
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, RECIPIENT, msg.as_string())

    print(f"  Sent to {RECIPIENT}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now_aest = datetime.now(AEST)
    time_slot = get_time_slot()

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest — {time_slot} — {day_date} ===\n")

    # ── Section 1 ──
    print("[1/5] Fetching RSS feeds...")
    raw = fetch_rss_articles()
    filtered = filter_articles(raw)
    print(f"      {len(raw)} fetched → {len(filtered)} after filtering")

    print("[2/5] Summarising news with Claude...")
    news_html = summarise_news(filtered, client)

    # ── Section 2 ──
    print("[3/5] Fetching forum threads...")
    reddit = fetch_reddit_threads()
    bigfooty = fetch_bigfooty_threads()
    print(f"      Reddit: {len(reddit)} threads | BigFooty: {len(bigfooty)} threads")

    print("[4/5] Summarising forum buzz with Claude...")
    forum_html = summarise_forum_buzz(reddit, bigfooty, client)

    # ── Section 3 ──
    print("[5/5] Running web catch-all search with Claude...")
    web_html = fetch_web_catchall(client)
    if web_html:
        print("      Novel content found — including section 3")
    else:
        print("      Nothing novel found — omitting section 3")

    # ── Email ──
    date_str = now_aest.strftime("%-d %B")
    day_str = now_aest.strftime("%A")
    subject = f"AFL Digest — {time_slot} — {day_str} {date_str}"

    html = build_email_html(news_html, forum_html, web_html, time_slot, now_aest)
    print(f"\nSending: {subject}")
    send_email(html, subject)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
