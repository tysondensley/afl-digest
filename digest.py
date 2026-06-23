#!/usr/bin/env python3
"""AFL news digest — fetches RSS and Reddit, scores articles by recency and
news-value keywords, then emails a curated summary.

No AI API required — fully free to run.
"""

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

# Patterns that indicate match-report / results content — penalised in scoring
_EXCLUDE_RE = re.compile(
    r"\b("
    r"score[sd]?|full[\s\-]?time|half[\s\-]?time|quarter[s]?|"
    r"goal[s]?|behind[s]?|defeated|beat\b|won\b|loss|lost\b|"
    r"highlights?|replay|live blog|as it happened|match report|match preview|"
    r"round \d+ results?"
    r")\b",
    re.IGNORECASE,
)

# Patterns that suggest genuine news — boosted in scoring
_INCLUDE_RE = re.compile(
    r"\b("
    r"trade[sd]?|trading|injur(y|ies|ed)|sign(ing|ed|s)?|contract|"
    r"suspension|suspended|deregistered|delisted|draft|recruit|"
    r"breaking|exclusive|interview|investigation|reveals?|confirms?|"
    r"opinion|analysis|verdict"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT = "Tyson.Densley@afl.com.au"

# How many hours back each time-slot edition looks
SLOT_LOOKBACK_HOURS = {"Morning": 9, "Midday": 6, "Afternoon": 5, "Evening": 5}


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
# RSS fetching & filtering
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
                        if (t.get("scheme") or "").endswith("creator"):
                            author = t.get("term", "")
                            break

                raw_title = getattr(entry, "title", "").strip()
                # Google News appends " - Source Name" to every title — strip it
                if " - " in raw_title:
                    raw_title = raw_title.rsplit(" - ", 1)[0].strip()

                # Strip leading "by"/"By " that some feeds include (with or without space)
                author = re.sub(r"(?i)^by\s*", "", author.strip()).strip()

                articles.append({
                    "source":    source,
                    "title":     raw_title,
                    "snippet":   re.sub(
                        r"<[^>]+>", "",
                        getattr(entry, "summary", getattr(entry, "description", ""))[:400]
                    ).strip(),
                    "link":      getattr(entry, "link", ""),
                    "author":    author,
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


def filter_by_recency(
    articles: list[dict],
    label: str = "",
    hours: int = 10,
    min_recent: int = 5,
) -> list[dict]:
    """
    Sort by publish date (newest first) and prefer articles from the last `hours` hours.
    Falls back to all articles only when fewer than `min_recent` recent ones exist.
    Use min_recent=1 to avoid falling back whenever at least one recent article is found.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    dated   = [a for a in articles if a.get("published")]
    undated = [a for a in articles if not a.get("published")]

    dated.sort(key=lambda a: a["published"], reverse=True)

    recent = [a for a in dated if a["published"] >= cutoff]
    older  = [a for a in dated if a["published"] <  cutoff]

    tag = f" [{label}]" if label else ""
    if len(recent) >= min_recent:
        print(f"      Recency{tag}: {len(recent)} from last {hours}h "
              f"(+ {len(older)} older, {len(undated)} undated discarded)")
        return recent + undated[:3]
    else:
        print(f"      Recency{tag}: only {len(recent)} recent in {hours}h — using all {len(articles)}")
        return dated + undated


# ---------------------------------------------------------------------------
# Author / thumbnail enrichment
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


def _scrape_og_image(url: str) -> str:
    """Fetch the Open Graph / Twitter Card image from a direct article URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")
        for attr in (
            {"property": "og:image"},
            {"property": "og:image:secure_url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
            {"itemprop": "image"},
        ):
            tag = soup.find("meta", attrs=attr)
            if tag and tag.get("content", "").startswith("http"):
                return tag["content"]
        link_tag = soup.find("link", rel="image_src")
        if link_tag and link_tag.get("href", "").startswith("http"):
            return link_tag["href"]
    except Exception:
        pass
    return ""


def enrich_thumbnails(articles: list[dict]) -> list[dict]:
    """
    For media articles without a thumbnail, concurrently scrape the article
    page for an Open Graph image.

    Google News redirect URLs use JS redirects so requests always lands on
    the Google News page itself — those are skipped to avoid the Google logo.
    """
    targets = [
        a for a in articles
        if not a.get("thumbnail")
        and a.get("link")
        and "news.google.com" not in a["link"]
    ]
    if not targets:
        return articles

    print(f"      Enriching thumbnails for {len(targets)} articles...")
    url_to_thumb: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_scrape_og_image, a["link"]): a["link"]
                   for a in targets[:15]}
        for fut in as_completed(futures, timeout=20):
            url = futures[fut]
            try:
                thumb = fut.result()
                if thumb:
                    url_to_thumb[url] = thumb
            except Exception:
                pass

    found = 0
    for a in articles:
        if a["link"] in url_to_thumb:
            a["thumbnail"] = url_to_thumb[a["link"]]
            found += 1
    print(f"      Found {found} thumbnails")
    return articles


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

def render_news_card(article: dict, bold_title: bool = True) -> str:
    """Render a single news item as a card with link, byline, and optional thumbnail."""
    url    = article["link"]
    source = article["source"]
    author = article.get("author", "")
    thumb  = article.get("thumbnail", "")
    title  = article["title"]

    byline = f'<span style="font-weight:600;">{source}</span>'
    if author:
        byline += f' &middot; <span style="font-style:italic;">{author}</span>'

    title_weight = "bold" if bold_title else "normal"
    link = (
        f'<a href="{url}" style="font-size:14px;font-weight:{title_weight};color:#003087;'
        f'text-decoration:none;line-height:1.45;display:block;">{title}</a>'
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
# Article selection — keyword + recency heuristic (no AI required)
# ---------------------------------------------------------------------------

def _score_article(article: dict, now: datetime) -> float:
    """
    Score an article by news-value keywords and recency.
    - _INCLUDE_RE match:  +5  (injury, signing, trade, exclusive, etc.)
    - _EXCLUDE_RE match:  -8  (match scores, highlights, results)
    - Recency bonus:      up to +8 for brand-new, 0 at ~20h old
    """
    text  = f"{article['title']} {article['snippet']}"
    score = 0.0
    if _INCLUDE_RE.search(text):
        score += 5.0
    if _EXCLUDE_RE.search(text):
        score -= 8.0
    if article.get("published"):
        age_hours = (now - article["published"]).total_seconds() / 3600
        score += max(0.0, 8.0 - age_hours * 0.4)   # 0 points at ~20 h old
    return score


def select_afl_official(articles: list[dict]) -> str:
    """Pick the top 5 AFL.com.au articles by heuristic score."""
    if not articles:
        return "<p>No AFL.com.au news found this period.</p>"

    now    = datetime.now(timezone.utc)
    scored = sorted(articles, key=lambda a: _score_article(a, now), reverse=True)
    top    = scored[:5]

    print(f"  AFL.com.au heuristic top {len(top)}: "
          + " | ".join(a["title"][:45] for a in top))
    return "\n".join(render_news_card(a, bold_title=True) for a in top)


def select_media_news(articles: list[dict]) -> str:
    """
    Pick the top 6–7 other-media articles by heuristic score,
    capping at 2 articles per source for variety.
    """
    if not articles:
        return "<p>No media news found this period.</p>"

    now    = datetime.now(timezone.utc)
    scored = sorted(articles, key=lambda a: _score_article(a, now), reverse=True)

    source_counts: dict[str, int] = {}
    selected: list[dict] = []
    for a in scored:
        src = a["source"]
        if source_counts.get(src, 0) < 2:
            selected.append(a)
            source_counts[src] = source_counts.get(src, 0) + 1
        if len(selected) == 7:
            break

    print(f"  Other media heuristic: {len(selected)} selected from {len(articles)}")
    return "\n".join(render_news_card(a, bold_title=False) for a in selected)


# ---------------------------------------------------------------------------
# Fan Forums (Reddit)
# ---------------------------------------------------------------------------

def fetch_reddit_threads() -> list[dict]:
    """
    Fetch r/AFL hot threads. Tries JSON API first (full metadata), then falls
    back to RSS via feedparser (title + URL only) if the IP is blocked.
    """
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
                print(f"      Reddit: JSON API succeeded ({base})")
                return threads
        except Exception as exc:
            print(f"  Reddit JSON warning ({base}): {exc}")

    for rss_base in ("https://www.reddit.com", "https://old.reddit.com"):
        rss_url = f"{rss_base}/r/AFL/hot.rss"
        try:
            feed = feedparser.parse(rss_url, request_headers=REDDIT_HEADERS)
            if not feed.entries:
                continue
            threads = []
            for entry in feed.entries[:10]:
                title = getattr(entry, "title", "").strip()
                url   = getattr(entry, "link",  "").strip()
                if not title or not url:
                    continue
                title = re.sub(r"^r/AFL:\s*", "", title)
                threads.append({
                    "title":    title,
                    "comments": None,
                    "score":    None,
                    "flair":    "",
                    "url":      url,
                })
                if len(threads) == 5:
                    break
            if threads:
                print(f"      Reddit: RSS fallback succeeded ({rss_base})")
                return threads
        except Exception as exc:
            print(f"  Reddit RSS warning ({rss_base}): {exc}")

    print("  Reddit: all methods failed — section will be empty")
    return []


def render_forum_section(reddit: list[dict]) -> str:
    """Render Reddit hot threads as a list of linked titles."""
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
            f'r/AFL'
            + (f' &nbsp;&middot;&nbsp; {t["comments"]:,} comments'
               f' &nbsp;&middot;&nbsp; {t["score"]:,} upvotes'
               if t["comments"] is not None else '')
            + '</p>'
            f'</div>'
        )
    return "\n".join(items)


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
    afl_html:   str,
    media_html: str,
    forum_html: str,
    time_slot:  str,
    now_aest:   datetime,
) -> str:
    day_date  = now_aest.strftime("%A %-d %B %Y")
    generated = now_aest.strftime("%-I:%M %p AEST")

    body_sections = (
        _section_row("Top Stories — AFL.com.au", afl_html, first=True)
        + _section_row("Top Stories — Other Media", media_html)
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
            <td style="background-color:#003087;padding:18px 28px 16px 28px;">
              <p style="margin:0;font-size:11px;font-weight:bold;color:#7faad4;
                         text-transform:uppercase;letter-spacing:0.1em;">AFL News Digest</p>
              <h1 style="margin:5px 0 0 0;font-size:22px;font-weight:bold;color:#ffffff;line-height:1.3;">
                {time_slot} &nbsp;&middot;&nbsp; {day_date}
              </h1>
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
    msg["From"]    = f"AFL News Digest <{gmail_user}>"
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, RECIPIENT, msg.as_string())

    print(f"  Sent to {RECIPIENT}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "v21"


def main() -> None:
    now_aest  = datetime.now(AEST)
    time_slot = get_time_slot()
    hours_lookback = SLOT_LOOKBACK_HOURS.get(time_slot, 6)

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest {SCRIPT_VERSION} — {time_slot} — {day_date} (lookback {hours_lookback}h) ===\n")

    # ── Section 1a: AFL.com.au ──
    print("[1/4] Fetching AFL.com.au feed...")
    afl_raw    = fetch_rss_articles(AFL_OFFICIAL_FEEDS)
    # min_recent=3: use only fresh articles when at least 3 exist within the window;
    # fall back to full pool (scored by recency) only when the feed is quiet.
    afl_pool   = filter_by_recency(afl_raw, "AFL.com.au", hours=hours_lookback, min_recent=3)
    afl_pool   = enrich_authors(afl_pool)
    print(f"      {len(afl_raw)} fetched → {len(afl_pool)} in pool")
    afl_html   = select_afl_official(afl_pool)

    # ── Section 1b: Other Media ──
    print("[2/4] Fetching other media feeds...")
    media_raw      = fetch_rss_articles(OTHER_MEDIA_FEEDS)
    media_filtered = filter_articles(media_raw)
    media_recent   = filter_by_recency(media_filtered, "media")
    media_recent   = enrich_thumbnails(media_recent)
    print(f"      {len(media_raw)} fetched → {len(media_filtered)} filtered → {len(media_recent)} recent")
    media_html = select_media_news(media_recent)

    # ── Section 2: Fan Forums ──
    print("[3/4] Fetching Reddit threads...")
    reddit     = fetch_reddit_threads()
    print(f"      {len(reddit)} threads")
    forum_html = render_forum_section(reddit)

    # ── Email ──
    print("[4/4] Sending email...")
    date_str = now_aest.strftime("%-d %B")

    # Use the top-scored headline as subject hook to drive opens
    now_utc = datetime.now(timezone.utc)
    all_articles = afl_pool + media_recent
    top = max(all_articles, key=lambda a: _score_article(a, now_utc), default=None)
    if top:
        headline = top["title"]
        if len(headline) > 65:
            headline = headline[:64].rstrip() + "…"
        subject = f"{headline} | {time_slot} {date_str}"
    else:
        subject = f"{time_slot} - {now_aest.strftime('%A')} {date_str}"

    html = build_email_html(afl_html, media_html, forum_html, time_slot, now_aest)
    print(f"      Subject: {subject}")
    send_email(html, subject)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
