#!/usr/bin/env python3
"""AFL news digest — fetches RSS, forum threads, and web search, then emails a summary."""

import os
import re
import smtplib
import time as _time
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
    Strip markdown code fences that Claude sometimes wraps around output.
    Handles: ```html, ```json, ```, and any stray backtick lines.
    """
    text = text.strip()
    # Remove opening fence line (```html, ```json, ``` etc.)
    text = re.sub(r"^`{3}[a-zA-Z]*\s*\n", "", text)
    # Remove closing fence line
    text = re.sub(r"\n`{3}\s*$", "", text)
    # Catch any remaining lone ``` at start or end
    text = re.sub(r"^`{3}", "", text).strip()
    text = re.sub(r"`{3}$", "", text).strip()
    return text


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


def _parse_entry_date(entry) -> datetime | None:
    """Return a UTC datetime for an RSS entry's publish time, or None."""
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime.fromtimestamp(_time.mktime(val), tz=timezone.utc)
            except Exception:
                pass
    return None


def fetch_rss_articles() -> list[dict]:
    articles = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            for entry in feed.entries[:25]:
                # Author: check every common location feedparser exposes
                author = ""
                if getattr(entry, "author_detail", None):
                    author = entry.author_detail.get("name", "")
                if not author:
                    author = getattr(entry, "author", "")
                if not author:
                    # Some feeds use Dublin Core creator
                    tags = getattr(entry, "tags", [])
                    for t in tags:
                        if t.get("scheme", "").endswith("creator"):
                            author = t.get("term", "")
                            break

                articles.append({
                    "source":    source,
                    "title":     getattr(entry, "title", "").strip(),
                    "snippet":   re.sub(
                        r"<[^>]+>", "",
                        getattr(entry, "summary", getattr(entry, "description", ""))[:400]
                    ).strip(),
                    "link":      getattr(entry, "link", ""),
                    "author":    author.strip(),
                    "thumbnail": get_thumbnail(entry),
                    "published": _parse_entry_date(entry),
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


def filter_by_recency(articles: list[dict]) -> list[dict]:
    """
    Sort by publish date (newest first) and prefer articles from the last
    8 hours — covering the gap since the previous digest run.
    Falls back to all articles if fewer than 5 recent ones exist.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=8)

    dated   = [a for a in articles if a.get("published")]
    undated = [a for a in articles if not a.get("published")]

    # Sort dated articles newest-first
    dated.sort(key=lambda a: a["published"], reverse=True)

    recent = [a for a in dated if a["published"] >= cutoff]
    older  = [a for a in dated if a["published"] < cutoff]

    if len(recent) >= 5:
        print(f"      Recency filter: {len(recent)} articles from last 8h "
              f"(+ {len(older)} older, {len(undated)} undated discarded)")
        return recent + undated[:3]   # allow a few undated as padding
    else:
        # Not enough recent content — use everything but keep newest first
        print(f"      Recency filter: only {len(recent)} recent — using all {len(articles)}")
        return dated + undated


def render_news_card(summary: str, article: dict) -> str:
    """Render a single news item as a card with link, byline, and optional thumbnail."""
    url    = article["link"]
    source = article["source"]
    author = article.get("author", "")
    thumb  = article.get("thumbnail", "")

    byline = f'<span style="font-weight:600;">{source}</span>'
    if author:
        byline += f' &middot; <span style="font-style:italic;">{author}</span>'

    link = (
        f'<a href="{url}" style="font-size:14px;font-weight:bold;color:#003087;'
        f'text-decoration:none;line-height:1.45;display:block;">{summary}</a>'
    )
    meta = f'<p style="margin:5px 0 0 0;font-size:17px;color:#888888;line-height:1.4;">{byline}</p>'

    if thumb:
        return (
            f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"'
            f' style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eeeeee;">'
            f'<tr>'
            f'<td width="76" valign="top" style="padding-right:14px;">'
            f'<a href="{url}" style="display:block;">'
            f'<img src="{thumb}" width="76" height="76" alt=""'
            f' style="border-radius:6px;display:block;width:76px;height:76px;object-fit:cover;border:0;">'
            f'</a></td>'
            f'<td valign="top">{link}{meta}</td>'
            f'</tr></table>'
        )
    else:
        return (
            f'<div style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eeeeee;">'
            f'{link}{meta}</div>'
        )


def summarise_news(articles: list[dict], client: anthropic.Anthropic) -> str:
    """
    Ask Claude to pick and summarise the 7 best stories using a simple
    INDEX|SUMMARY pipe format — reliable, no JSON or HTML for Claude to mangle.
    We look up source / author / link / thumbnail from our own data by index.
    """
    if not articles:
        return "<p>No relevant news found in this period.</p>"

    pool = articles[:20]
    numbered = "\n".join(
        f"[{i}] {a['title']} — {a['snippet'][:180]}"
        for i, a in enumerate(pool)
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=(
            "You are a senior AFL editor. From the numbered articles below, pick the 7 most "
            "newsworthy (trades, injuries, signings, contracts, suspensions — NOT match results "
            "or club PR).\n\n"
            "Return EXACTLY 7 lines. Each line must follow this format:\n"
            "INDEX|One sentence summary of the story.\n\n"
            "Example output:\n"
            "2|Geelong have re-signed key defender Sam Taylor after injury concerns.\n"
            "5|Carlton confirm a four-person panel to select Michael Voss's replacement.\n\n"
            "Rules:\n"
            "- No other text, no headings, no blank lines, no markdown, no HTML.\n"
            "- INDEX is the number in square brackets from the input.\n"
            "- Summary is plain text only, one sentence."
        ),
        messages=[{"role": "user", "content": numbered}],
    )

    raw = response.content[0].text.strip()
    print(f"  Claude news raw response:\n{raw}\n")

    cards: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        idx_str, _, summary = line.partition("|")
        # Strip any stray brackets, spaces, bullets
        idx_str = re.sub(r"[^\d]", "", idx_str)
        summary = summary.strip()
        if not idx_str or not summary:
            continue
        idx = int(idx_str)
        if 0 <= idx < len(pool):
            cards.append(render_news_card(summary, pool[idx]))

    if not cards:
        print("  Warning: no cards parsed — falling back to plain list")
        return "<p>News summarisation unavailable this period.</p>"

    return "\n".join(cards)


# ---------------------------------------------------------------------------
# Section 2 — Forum buzz
# ---------------------------------------------------------------------------

def fetch_reddit_threads() -> list[dict]:
    url = "https://www.reddit.com/r/AFL/hot.json?limit=15"
    try:
        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        threads = []
        for post in resp.json()["data"]["children"]:
            p = post["data"]
            if p.get("stickied"):          # skip mod/pinned posts
                continue
            threads.append({
                "title":    p.get("title", ""),
                "comments": p.get("num_comments", 0),
                "score":    p.get("score", 0),
                "flair":    p.get("link_flair_text", "") or "",
                "url":      f"https://www.reddit.com{p.get('permalink', '')}",
            })
            if len(threads) == 5:
                break
        return threads
    except Exception as exc:
        print(f"  Reddit warning: {exc}")
        return []


def render_forum_section(reddit: list[dict]) -> str:
    """Render Reddit hot threads directly — no Claude summarisation needed."""
    if not reddit:
        return "<p style='font-size:14px;color:#555;'>No Reddit threads available this period.</p>"

    items = []
    for t in reddit:
        flair_html = ""
        if t["flair"]:
            flair_html = (
                f' <span style="font-size:11px;color:#ffffff;background:#003087;'
                f'border-radius:3px;padding:1px 5px;vertical-align:middle;">'
                f'{t["flair"]}</span>'
            )
        items.append(
            f'<div style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #eeeeee;">'
            f'<a href="{t["url"]}" style="font-size:14px;font-weight:bold;color:#003087;'
            f'text-decoration:none;line-height:1.4;display:block;">'
            f'{t["title"]}</a>{flair_html}'
            f'<p style="margin:5px 0 0 0;font-size:12px;color:#888888;">'
            f'r/AFL &nbsp;&middot;&nbsp; {t["comments"]:,} comments &nbsp;&middot;&nbsp; '
            f'{t["score"]:,} upvotes</p>'
            f'</div>'
        )
    return "\n".join(items)


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
        "Otherwise output ONLY a valid HTML <ul> with 3–5 <li> items. "
        "Each <li> must be short: a bold headline phrase, then a single sentence of context. "
        "Maximum two lines per item. No markdown. No code fences. No surrounding text."
    )
    user_prompt = (
        "Search for significant AFL news from the past 6 hours not covered by "
        "AFL.com.au, Fox Footy, The Age, or Herald Sun RSS feeds. "
        "Breaking controversies, viral moments, social media stories, "
        "anything mainstream AFL media is slow on. "
        "Keep each finding to: bold headline + one sentence max. "
        "If nothing novel found, reply NOTHING."
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

                <!-- Section 1: Top Stories -->
                <tr>
                  <td style="padding:22px 0 8px 0;">
                    <h2 style="margin:0 0 16px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      Top Stories
                    </h2>
                    {news_html}
                  </td>
                </tr>

                <!-- Section 2: Fan Forums -->
                <tr>
                  <td style="padding:20px 0 8px 0;border-top:1px solid #eeeeee;">
                    <h2 style="margin:0 0 14px 0;font-size:16px;font-weight:bold;
                               color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                      Fan Forums
                    </h2>
                    {forum_html}
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
                Automated AFL Digest &middot; Generated {generated} &middot; {SCRIPT_VERSION}
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

SCRIPT_VERSION = "v5"


def main() -> None:
    client    = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now_aest  = datetime.now(AEST)
    time_slot = get_time_slot()

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest {SCRIPT_VERSION} — {time_slot} — {day_date} ===\n")

    # ── Section 1 ──
    print("[1/5] Fetching RSS feeds...")
    raw      = fetch_rss_articles()
    filtered = filter_articles(raw)
    recent   = filter_by_recency(filtered)
    print(f"      {len(raw)} fetched → {len(filtered)} filtered → {len(recent)} recent")

    print("[2/5] Summarising news with Claude...")
    news_html = summarise_news(recent, client)

    # ── Section 2 ──
    print("[3/5] Fetching Reddit threads...")
    reddit     = fetch_reddit_threads()
    print(f"      {len(reddit)} threads")
    forum_html = render_forum_section(reddit)

    # ── Section 3 ──
    print("[4/4] Running web catch-all search with Claude...")
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
