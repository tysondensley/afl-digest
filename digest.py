#!/usr/bin/env python3
"""AFL news digest — fetches RSS, forum threads, and web search, then emails a summary."""

import json
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

# Patterns that suggest genuine news — boosted in priority
_INCLUDE_RE = re.compile(
    r"\b("
    r"trade[sd]?|trading|injur(y|ies|ed)|sign(ing|ed|s)?|contract|"
    r"suspension|suspended|deregistered|delisted|draft|recruit|"
    r"breaking|exclusive|interview|investigation|reveals?|confirms?"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT = "Tyson.Densley@afl.com.au"
MODEL     = "claude-sonnet-4-6"


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
# Shared utility
# ---------------------------------------------------------------------------

def clean_claude_html(text: str) -> str:
    """
    Strip markdown code fences that Claude sometimes wraps around HTML/JSON output.
    e.g. ```html\\n<ul>...</ul>\\n``` → <ul>...</ul>
    """
    text = text.strip()
    text = re.sub(r"^```(?:html|json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Section 1 — RSS news
# ---------------------------------------------------------------------------

def get_thumbnail(entry) -> str:
    """Extract the best available image URL from a feedparser entry."""
    try:
        if getattr(entry, "media_thumbnail", None):
            return entry.media_thumbnail[0].get("url", "")
        if getattr(entry, "media_content", None):
            for m in entry.media_content:
                url = m.get("url", "")
                if m.get("type", "").startswith("image") or re.search(
                    r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I
                ):
                    return url
        if getattr(entry, "enclosures", None):
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("image"):
                    return enc.get("href", enc.get("url", ""))
        # Some feeds put images in links
        if getattr(entry, "links", None):
            for link in entry.links:
                if link.get("type", "").startswith("image"):
                    return link.get("href", "")
    except Exception:
        pass
    return ""


def fetch_rss_articles() -> list[dict]:
    articles = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            for entry in feed.entries[:25]:
                # Author: try author_detail first, fall back to author string
                author = ""
                if getattr(entry, "author_detail", None):
                    author = entry.author_detail.get("name", "")
                if not author:
                    author = entry.get("author", "")

                articles.append({
                    "source":    source,
                    "title":     entry.get("title", "").strip(),
                    "snippet":   re.sub(
                        r"<[^>]+>", "",
                        entry.get("summary", entry.get("description", ""))[:400]
                    ).strip(),
                    "link":      entry.get("link", ""),
                    "author":    author.strip(),
                    "thumbnail": get_thumbnail(entry),
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
    # Genuine news first; deduplicate by title prefix
    seen: set[str] = set()
    result = []
    for a in kept + deprioritised:
        key = a["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            result.append(a)
    return result


def summarise_news(articles: list[dict], client: anthropic.Anthropic) -> str:
    """
    Ask Claude to pick the 7 best stories and return structured JSON.
    We render the final HTML ourselves so we control links, source, author, thumbnail.
    """
    if not articles:
        return "<p>No relevant news found in this period.</p>"

    items_input = [
        {
            "id":      i,
            "source":  a["source"],
            "author":  a["author"],
            "url":     a["link"],
            "title":   a["title"],
            "snippet": a["snippet"],
        }
        for i, a in enumerate(articles[:30])
    ]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=(
            "You are a senior AFL editor. From the articles provided, select the 7 most newsworthy.\n"
            "Focus on: trades, injuries, signings, contracts, suspensions, breaking news.\n"
            "Exclude: match scores, results, club promotional content.\n"
            "Return a JSON array of exactly 7 objects. Each object must have these exact keys:\n"
            '  "summary" — one clear sentence summarising the story (plain text, no HTML)\n'
            '  "source"  — copy the source field exactly as provided\n'
            '  "author"  — copy the author field exactly as provided (empty string if blank)\n'
            '  "url"     — copy the url field exactly as provided\n'
            "Return ONLY the raw JSON array. No markdown. No code fences. No explanation."
        ),
        messages=[{"role": "user", "content": json.dumps(items_input, ensure_ascii=False)}],
    )

    raw = clean_claude_html(response.content[0].text)

    try:
        selected = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Graceful fallback — render Claude's raw text as a plain paragraph
        return f'<p style="font-size:14px;color:#333;line-height:1.6;">{raw}</p>'

    # Build a URL → thumbnail lookup from the original fetched articles
    thumb_map = {a["link"]: a["thumbnail"] for a in articles}

    cards: list[str] = []
    for item in selected[:7]:
        summary = item.get("summary", "").strip()
        source  = item.get("source", "").strip()
        author  = item.get("author", "").strip()
        url     = item.get("url", "#").strip()
        thumb   = thumb_map.get(url, "")

        byline  = f'<span style="font-weight:600;">{source}</span>'
        if author:
            byline += f' &middot; <span style="font-style:italic;">{author}</span>'

        link_open  = f'<a href="{url}" style="font-size:14px;font-weight:bold;color:#003087;text-decoration:none;line-height:1.45;display:block;">'
        link_close = "</a>"
        meta       = f'<p style="margin:5px 0 0 0;font-size:11px;color:#888888;line-height:1.4;">{byline}</p>'

        if thumb:
            card = (
                f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"'
                f' style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eeeeee;">'
                f'<tr>'
                f'<td width="76" valign="top" style="padding-right:14px;">'
                f'<a href="{url}" style="display:block;">'
                f'<img src="{thumb}" width="76" height="76" alt="" style="border-radius:6px;display:block;'
                f'width:76px;height:76px;object-fit:cover;border:0;">'
                f'</a>'
                f'</td>'
                f'<td valign="top">'
                f'{link_open}{summary}{link_close}'
                f'{meta}'
                f'</td>'
                f'</tr>'
                f'</table>'
            )
        else:
            card = (
                f'<div style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eeeeee;">'
                f'{link_open}{summary}{link_close}'
                f'{meta}'
                f'</div>'
            )

        cards.append(card)

    return "\n".join(cards)


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
                "title":    p.get("title", ""),
                "comments": p.get("num_comments", 0),
                "score":    p.get("score", 0),
                "flair":    p.get("link_flair_text", ""),
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
            "For each bullet: name the topic and note the general fan sentiment "
            "(excited, frustrated, divided, etc.). "
            "Return ONLY a valid HTML <ul> element with 3–5 <li> items. "
            "No markdown. No code fences. No surrounding text."
        ),
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return clean_claude_html(response.content[0].text)


# ---------------------------------------------------------------------------
# Section 3 — Web catch-all
# ---------------------------------------------------------------------------

def fetch_web_catchall(client: anthropic.Anthropic) -> str | None:
    """
    Ask Claude to search the web for AFL news from the last 6 hours not covered
    by mainstream RSS feeds. Returns an HTML bullet list, or None if nothing novel.
    """
    system = (
        "You are an AFL news scout. Search for significant AFL news or stories "
        "from the last 6 hours that are unlikely to appear in mainstream AFL RSS feeds — "
        "breaking stories, controversies, viral content, player social media incidents, "
        "or under-reported news. "
        "If you find nothing genuinely novel or significant, output only the single word: NOTHING. "
        "Otherwise output ONLY a valid HTML <ul> with 3–5 <li> bullet points. "
        "No markdown. No code fences. No surrounding text."
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
                        text = clean_claude_html(block.text)
                        if not text or text.upper() == "NOTHING":
                            return None
                        return text
                return None

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = [
                    {"type": "tool_result", "tool_use_id": block.id, "content": ""}
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
    day_date  = now_aest.strftime("%A %-d %B %Y")
    generated = now_aest.strftime("%-I:%M %p AEST")

    ul_style = (
        "margin:0;padding:0 0 0 20px;font-size:14px;"
        "line-height:1.7;color:#333333;"
    )
    li_style = "margin-bottom:8px;"

    def style_list(raw: str) -> str:
        """Inject inline styles onto <ul>/<li> so Gmail renders them correctly."""
        raw = re.sub(r"<ul([^>]*)>",  f'<ul style="{ul_style}">',  raw)
        raw = re.sub(r"<li([^>]*)>",  f'<li style="{li_style}">',  raw)
        return raw

    web_section = ""
    if web_html:
        web_section = f"""
          <!-- Section 3: Breaking / Viral -->
          <tr>
            <td style="padding:20px 0 8px 0;border-top:1px solid #eeeeee;">
              <h2 style="margin:0 0 14px 0;font-size:16px;font-weight:bold;
                         color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                Breaking / Viral
              </h2>
              {style_list(web_html)}
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
              <p style="margin:0;font-size:11px;font-weight:bold;color:#7faad4;
                         text-transform:uppercase;letter-spacing:0.1em;">AFL Digest</p>
              <h1 style="margin:4px 0 0 0;font-size:26px;font-weight:bold;color:#ffffff;">
                {time_slot}
              </h1>
              <p style="margin:4px 0 0 0;font-size:13px;color:#a8c4e0;">{day_date}</p>
            </td>
          </tr>

          <!-- ── Body ── -->
          <tr>
            <td style="padding:0 28px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">

                <!-- Section 1: News & Transfers -->
                <tr>
                  <td style="padding:22px 0 8px 0;">
                    <h2 style="margin:0 0 16px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      News &amp; Transfers
                    </h2>
                    {news_html}
                  </td>
                </tr>

                <!-- Section 2: Fan Buzz -->
                <tr>
                  <td style="padding:20px 0 8px 0;border-top:1px solid #eeeeee;">
                    <h2 style="margin:0 0 14px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      Fan Buzz
                    </h2>
                    {style_list(forum_html)}
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
    gmail_user     = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"AFL Digest <{gmail_user}>"
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, RECIPIENT, msg.as_string())

    print(f"  Sent to {RECIPIENT}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    client    = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now_aest  = datetime.now(AEST)
    time_slot = get_time_slot()

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest — {time_slot} — {day_date} ===\n")

    # ── Section 1 ──
    print("[1/5] Fetching RSS feeds...")
    raw      = fetch_rss_articles()
    filtered = filter_articles(raw)
    print(f"      {len(raw)} fetched → {len(filtered)} after filtering")

    print("[2/5] Summarising news with Claude...")
    news_html = summarise_news(filtered, client)

    # ── Section 2 ──
    print("[3/5] Fetching forum threads...")
    reddit   = fetch_reddit_threads()
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
    day_str  = now_aest.strftime("%A")
    subject  = f"AFL Digest — {time_slot} — {day_str} {date_str}"

    html = build_email_html(news_html, forum_html, web_html, time_slot, now_aest)
    print(f"\nSending: {subject}")
    send_email(html, subject)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
