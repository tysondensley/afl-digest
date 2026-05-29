#!/usr/bin/env python3
"""AFL news digest — fetches RSS, forum threads, and web search, then emails a summary."""

import os
import re
import smtplib
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# AFL's own feed — gets its own dedicated section
AFL_OFFICIAL_FEEDS = [
    ("AFL.com.au", "https://www.afl.com.au/rss"),
]

# Media outlets — pooled into a single "Other Media" section.
# Direct RSS blocked for Herald Sun / Fox Footy / paywalled outlets;
# Google News RSS proxies their headlines without hitting the paywall.
OTHER_MEDIA_FEEDS = [
    ("The Age",          "https://www.theage.com.au/rss/sport/afl.xml"),
    ("The Guardian",     "https://www.theguardian.com/sport/afl/rss"),
    ("Herald Sun",       "https://news.google.com/rss/search?q=AFL+site:heraldsun.com.au&hl=en-AU&gl=AU&ceid=AU:en"),
    ("Fox Footy",        "https://news.google.com/rss/search?q=AFL+site:foxsports.com.au&hl=en-AU&gl=AU&ceid=AU:en"),
    ("ABC News",         "https://news.google.com/rss/search?q=AFL+site:abc.net.au&hl=en-AU&gl=AU&ceid=AU:en"),
    ("SEN",              "https://news.google.com/rss/search?q=AFL+site:sen.com.au&hl=en-AU&gl=AU&ceid=AU:en"),
    ("The Australian",   "https://news.google.com/rss/search?q=AFL+site:theaustralian.com.au&hl=en-AU&gl=AU&ceid=AU:en"),
    ("AFR",              "https://news.google.com/rss/search?q=AFL+site:afr.com&hl=en-AU&gl=AU&ceid=AU:en"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    r"breaking|exclusive|interview|investigation|reveals?|confirms?|"
    r"opinion|analysis|verdict|verdict"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT = "Tyson.Densley@afl.com.au"
MODEL     = "claude-sonnet-4-6"

# AFL journalists to monitor on X for the Journo Top Tweets section
JOURNALISTS = [
    ("Cal Twomey",      "@CalTwomey"),
    ("Mitch Cleary",    "@mitchcleary"),
    ("Tom Morris",      "@tommorris32"),
    ("Xander McGuire",  "@xandermcguire"),
    ("Ryan Daniels",    "@ryandaniels_"),
    ("Riley Beveridge", "@RileyBeveridge"),
    ("Josh Gabelich",   "@JoshGabelich"),
    ("Michael Whiting", "@WhitingAFL"),
    ("Nathan Schmook",  "@NathanSchmook"),
    ("Emily Patterson", "@empaterson"),
    ("Jay Clark",       "@JayClark7"),
    ("Jon Ralph",       "@JonRalphHL"),
]


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
        return "Morning"
    if hour < 14:
        return "Midday"
    if hour < 20:
        return "Afternoon"
    return "Evening"


# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def clean_claude_html(text: str) -> str:
    """Strip markdown code fences that Claude sometimes wraps around output."""
    text = text.strip()
    text = re.sub(r"^`{3}[a-zA-Z]*\s*\n", "", text)
    text = re.sub(r"\n`{3}\s*$", "", text)
    text = re.sub(r"^`{3}", "", text).strip()
    text = re.sub(r"`{3}$", "", text).strip()
    return text


# ---------------------------------------------------------------------------
# RSS fetching & filtering (shared by both sections)
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


def fetch_rss_articles(feeds: list[tuple]) -> list[dict]:
    """Fetch and parse RSS articles from a list of (source, url) tuples."""
    articles = []
    for source, url in feeds:
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
                    tags = getattr(entry, "tags", [])
                    for t in tags:
                        if t.get("scheme", "").endswith("creator"):
                            author = t.get("term", "")
                            break

                raw_title = getattr(entry, "title", "").strip()
                # Google News appends " - Source Name" to every title — strip it
                if " - " in raw_title:
                    raw_title = raw_title.rsplit(" - ", 1)[0].strip()

                articles.append({
                    "source":    source,
                    "title":     raw_title,
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


def filter_by_recency(articles: list[dict], label: str = "") -> list[dict]:
    """
    Sort by publish date (newest first) and prefer articles from the last 10 hours.
    Falls back to all articles if fewer than 5 recent ones exist.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=10)

    dated   = [a for a in articles if a.get("published")]
    undated = [a for a in articles if not a.get("published")]

    dated.sort(key=lambda a: a["published"], reverse=True)

    recent = [a for a in dated if a["published"] >= cutoff]
    older  = [a for a in dated if a["published"] <  cutoff]

    tag = f" [{label}]" if label else ""
    if len(recent) >= 5:
        print(f"      Recency{tag}: {len(recent)} from last 10h "
              f"(+ {len(older)} older, {len(undated)} undated discarded)")
        return recent + undated[:3]
    else:
        print(f"      Recency{tag}: only {len(recent)} recent — using all {len(articles)}")
        return dated + undated


# ---------------------------------------------------------------------------
# Author enrichment (AFL.com.au only)
# ---------------------------------------------------------------------------

def _scrape_afl_author(url: str) -> str:
    """Try to extract a byline from an AFL.com.au article page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6)
        soup = BeautifulSoup(resp.text, "lxml")
        for sel in (
            "meta[name='author']",
            "[class*='author-name']",
            "[class*='ArticleAuthor']",
            "[class*='byline']",
            "[class*='contributor']",
            "span.name",
        ):
            el = soup.select_one(sel)
            if el:
                val = el.get("content") or el.get_text(strip=True)
                if val and 2 < len(val) < 80:
                    return val.strip()
    except Exception:
        pass
    return ""


def enrich_authors(articles: list[dict]) -> list[dict]:
    """
    For AFL.com.au articles without an author, concurrently scrape the article
    page to find the byline.
    """
    targets = [
        a for a in articles
        if a["source"] == "AFL.com.au" and not a["author"] and a["link"]
    ]
    if not targets:
        return articles

    print(f"      Enriching authors for {len(targets)} AFL.com.au articles...")
    url_to_author: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_scrape_afl_author, a["link"]): a["link"]
                   for a in targets[:10]}
        for fut in as_completed(futures, timeout=15):
            url = futures[fut]
            try:
                author = fut.result()
                if author:
                    url_to_author[url] = author
            except Exception:
                pass

    for a in articles:
        if a["link"] in url_to_author:
            a["author"] = url_to_author[a["link"]]
    return articles


# ---------------------------------------------------------------------------
# News card rendering
# ---------------------------------------------------------------------------

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
    meta = (
        f'<p style="margin:5px 0 0 0;font-size:13px;color:#888888;line-height:1.4;">'
        f'{byline}</p>'
    )

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


# ---------------------------------------------------------------------------
# Section 1a — AFL.com.au top stories
# ---------------------------------------------------------------------------

def summarise_afl_official(articles: list[dict], client: anthropic.Anthropic) -> str:
    """Summarise AFL.com.au articles — official club/league news, 4–5 items."""
    if not articles:
        return "<p>No AFL.com.au news found this period.</p>"

    pool = articles[:15]
    numbered = "\n".join(
        f"[{i}] {a['title']} — {a['snippet'][:180]}"
        for i, a in enumerate(pool)
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=(
            "You are a senior AFL editor reviewing stories from AFL.com.au. "
            "Pick the 4–5 most newsworthy items — focus on injuries, team announcements, "
            "signings, suspensions, rule changes, and official league news. "
            "Skip pure match previews, club PR fluff, and generic round wrap-ups.\n\n"
            "Return EXACTLY 4–5 lines in this format:\n"
            "INDEX|One sentence summary.\n\n"
            "Rules: no other text, no headings, no blank lines, no markdown, no HTML. "
            "INDEX is the bracket number from the input. Summary is plain text only."
        ),
        messages=[{"role": "user", "content": numbered}],
    )

    raw = response.content[0].text.strip()
    print(f"  Claude AFL.com.au raw:\n{raw}\n")
    return _parse_index_pipe_response(raw, pool)


# ---------------------------------------------------------------------------
# Section 1b — Other media top stories
# ---------------------------------------------------------------------------

def summarise_media_news(articles: list[dict], client: anthropic.Anthropic) -> str:
    """
    Summarise stories from the wider media pool — strong bias toward exclusives,
    breaking news, and opinion. Deprioritise rewrites of existing stories.
    """
    if not articles:
        return "<p>No media news found this period.</p>"

    pool = articles[:30]
    numbered = "\n".join(
        f"[{i}] ({a['source']}) {a['title']} — {a['snippet'][:180]}"
        for i, a in enumerate(pool)
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=(
            "You are a senior AFL editor curating the best journalism from across Australian media. "
            "Pick the 6–7 most valuable stories from the list below.\n\n"
            "STRONG preference for:\n"
            "- EXCLUSIVES and BREAKING news (scoops, first reports, inside sources)\n"
            "- OPINION and ANALYSIS pieces with a strong take or original argument\n"
            "- INVESTIGATIONS, controversies, and governance stories\n"
            "- Injury updates, trade whispers, and contract news\n\n"
            "DEPRIORITISE:\n"
            "- Rewrites or aggregations of stories already broken by other outlets\n"
            "- Generic match previews and round summaries\n"
            "- Club PR and promotional content\n\n"
            "Return EXACTLY 6–7 lines in this format:\n"
            "INDEX|One sentence summary.\n\n"
            "Rules: no other text, no headings, no blank lines, no markdown, no HTML. "
            "INDEX is the bracket number from the input. Summary is plain text only."
        ),
        messages=[{"role": "user", "content": numbered}],
    )

    raw = response.content[0].text.strip()
    print(f"  Claude media news raw:\n{raw}\n")
    return _parse_index_pipe_response(raw, pool)


def _parse_index_pipe_response(raw: str, pool: list[dict]) -> str:
    """Parse Claude's INDEX|SUMMARY lines into rendered news cards."""
    cards: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        idx_str, _, summary = line.partition("|")
        idx_str = re.sub(r"[^\d]", "", idx_str)
        summary = summary.strip()
        if not idx_str or not summary:
            continue
        idx = int(idx_str)
        if 0 <= idx < len(pool):
            cards.append(render_news_card(summary, pool[idx]))

    if not cards:
        print("  Warning: no cards parsed — falling back")
        return "<p>News summarisation unavailable this period.</p>"

    return "\n".join(cards)


# ---------------------------------------------------------------------------
# Section 3 — Fan Forums (Reddit)
# ---------------------------------------------------------------------------

def fetch_reddit_threads() -> list[dict]:
    """Try old.reddit.com first (less likely to block cloud IPs), fall back to www."""
    for base in ("https://old.reddit.com", "https://www.reddit.com"):
        url = f"{base}/r/AFL/hot.json?limit=15"
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
            resp.raise_for_status()
            threads = []
            for post in resp.json()["data"]["children"]:
                p = post["data"]
                if p.get("stickied"):
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
            if threads:
                return threads
        except Exception as exc:
            print(f"  Reddit warning ({base}): {exc}")
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
# Section 2 — Journo Top Tweets
# ---------------------------------------------------------------------------

def _run_agentic_search(client: anthropic.Anthropic, system: str, user_prompt: str) -> str:
    """Run a Claude web-search agentic loop and return the final text response."""
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    for _ in range(8):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=system,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return ""
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in response.content if b.type == "tool_use"
            ]
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
    return ""


def fetch_journalist_tweets(client: anthropic.Anthropic, hours: int = 6) -> str | None:
    """
    Use Claude web search to find recent AFL tweets from known journalists.
    Returns rendered HTML card string, or None if nothing found.
    """
    names_list = ", ".join(f"{n} ({h})" for n, h in JOURNALISTS)
    handles    = " OR ".join(h for _, h in JOURNALISTS)

    system = (
        "You are an AFL media analyst. Search X (Twitter) for recent tweets from specific "
        "AFL journalists. Only include tweets that are genuinely newsworthy — breaking news, "
        "injury updates, trade whispers, controversies, or strong takes getting traction. "
        "Ignore retweets, replies, and promotional content. "
        f"Only include tweets posted in the last {hours} hours. "
        "If you find relevant tweets, return them as lines in this EXACT format "
        "(use <<< as the delimiter — never a pipe character):\n"
        "FULL_NAME<<<@HANDLE<<<TWEET_TEXT<<<TWEET_URL\n\n"
        "Return 3–5 lines maximum. If you find nothing newsworthy, return only: NOTHING\n"
        "No other text. No markdown. No explanation."
    )
    user_prompt = (
        f"Search X/Twitter for AFL-related tweets posted in the last {hours} hours from "
        f"these journalists: {names_list}. "
        f"You can search: site:x.com ({handles}) AFL\n"
        "Return only newsworthy tweets (breaking news, injuries, trades, controversies) "
        "in the format: FULL_NAME<<<@HANDLE<<<TWEET_TEXT<<<TWEET_URL\n"
        "If nothing relevant found, reply NOTHING."
    )

    try:
        raw = _run_agentic_search(client, system, user_prompt)
        print(f"  Journalist tweets raw:\n{raw[:300]}\n")
        if not raw or raw.upper() == "NOTHING":
            return None
        return _render_tweet_cards(raw)
    except Exception as exc:
        print(f"  Journalist tweets warning: {exc}")
        return None


def _render_tweet_cards(raw: str) -> str | None:
    """Parse <<< -delimited tweet lines and render as HTML cards."""
    cards = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if "<<<" not in line:
            continue
        parts = [p.strip() for p in line.split("<<<")]
        if len(parts) < 3:
            continue
        name       = parts[0]
        handle     = parts[1] if len(parts) > 1 else ""
        tweet_text = parts[2] if len(parts) > 2 else ""
        tweet_url  = parts[3] if len(parts) > 3 else "#"

        if not name or not tweet_text:
            continue

        cards.append(
            f'<div style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #eeeeee;">'
            f'<p style="margin:0 0 5px 0;font-size:12px;font-weight:bold;color:#1d9bf0;">'
            f'{name} <span style="font-weight:normal;color:#999999;">{handle}</span></p>'
            f'<p style="margin:0 0 7px 0;font-size:14px;color:#333333;line-height:1.55;">'
            f'{tweet_text}</p>'
            f'<a href="{tweet_url}" style="font-size:11px;color:#999999;text-decoration:none;">'
            f'View on X &rarr;</a>'
            f'</div>'
        )

    return "\n".join(cards) if cards else None


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

def _section_row(heading: str, content: str, first: bool = False) -> str:
    """Render a named section row for the email body table."""
    top_padding = "22px" if first else "20px"
    border      = "" if first else "border-top:1px solid #eeeeee;"
    return f"""
              <tr>
                <td style="padding:{top_padding} 0 8px 0;{border}">
                  <h2 style="margin:0 0 14px 0;font-size:16px;font-weight:bold;
                             color:#003087;text-transform:uppercase;letter-spacing:0.05em;">
                    {heading}
                  </h2>
                  {content}
                </td>
              </tr>"""


def build_email_html(
    afl_html:    str,
    media_html:  str,
    tweets_html: str | None,
    forum_html:  str,
    time_slot:   str,
    now_aest:    datetime,
) -> str:
    day_date  = now_aest.strftime("%A %-d %B %Y")
    generated = now_aest.strftime("%-I:%M %p AEST")

    tweets_section = ""
    if tweets_html:
        tweets_section = _section_row("Journo Top Tweets", tweets_html)

    body_sections = (
        _section_row("Top Stories — AFL.com.au", afl_html,   first=True)
        + _section_row("Top Stories — Other Media", media_html)
        + tweets_section
        + _section_row("Fan Forums", forum_html)
    )

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
                         text-transform:uppercase;letter-spacing:0.1em;">AFL News Digest</p>
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
                {body_sections}
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

SCRIPT_VERSION = "v7"


def main() -> None:
    client    = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now_aest  = datetime.now(AEST)
    time_slot = get_time_slot()

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest {SCRIPT_VERSION} — {time_slot} — {day_date} ===\n")

    # ── Section 1a: AFL.com.au ──
    print("[1/5] Fetching AFL.com.au feed...")
    afl_raw      = fetch_rss_articles(AFL_OFFICIAL_FEEDS)
    afl_filtered = filter_articles(afl_raw)
    afl_recent   = filter_by_recency(afl_filtered, "AFL.com.au")
    afl_recent   = enrich_authors(afl_recent)
    print(f"      {len(afl_raw)} fetched → {len(afl_filtered)} filtered → {len(afl_recent)} recent")

    print("[2/5] Summarising AFL.com.au stories with Claude...")
    afl_html = summarise_afl_official(afl_recent, client)

    # ── Section 1b: Other Media ──
    print("[3/5] Fetching other media feeds...")
    media_raw      = fetch_rss_articles(OTHER_MEDIA_FEEDS)
    media_filtered = filter_articles(media_raw)
    media_recent   = filter_by_recency(media_filtered, "media")
    print(f"      {len(media_raw)} fetched → {len(media_filtered)} filtered → {len(media_recent)} recent")

    print("[3b]  Summarising media stories with Claude (bias: exclusives/breaking/opinion)...")
    media_html = summarise_media_news(media_recent, client)

    # ── Section 2: Journo Top Tweets ──
    print("[4/5] Fetching journalist tweets via Claude web search...")
    tweets_html = fetch_journalist_tweets(client)
    if tweets_html:
        print("      Journalist tweets found — including section")
    else:
        print("      No journalist tweets found — omitting section")

    # ── Section 3: Fan Forums ──
    print("[5/5] Fetching Reddit threads...")
    reddit     = fetch_reddit_threads()
    print(f"      {len(reddit)} threads")
    forum_html = render_forum_section(reddit)

    # ── Email ──
    date_str = now_aest.strftime("%-d %B")
    day_str  = now_aest.strftime("%A")
    subject  = f"AFL News Digest — {time_slot} — {day_str} {date_str}"

    html = build_email_html(afl_html, media_html, tweets_html, forum_html, time_slot, now_aest)
    print(f"\nSending: {subject}")
    send_email(html, subject)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
