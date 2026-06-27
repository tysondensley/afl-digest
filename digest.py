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

AFL_OFFICIAL_FEEDS = [
    ("AFL.com.au", "https://www.afl.com.au/rss"),
]

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

# AFL.com.au YouTube channel RSS
AFL_YOUTUBE_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id=UCVhg8kpRh6WXzihfaXsgw7Q"

BIGFOOTY_URL = "https://www.bigfooty.com/forum/forums/afl-football.1/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

REDDIT_HEADERS = {
    "User-Agent": "AFL-Digest/1.0 (personal newsletter; by /u/afl_digest_bot)"
}

# Patterns that indicate match-report / results content
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

# At least one of these must appear for an article to pass the AFL relevance gate.
# Catches non-AFL articles that slip through Google News RSS searches.
_AFL_RE = re.compile(
    r"\b("
    r"AFL|AFLW|VFL|VFLW|footy|football|"
    r"Carlton|Collingwood|Richmond|Hawthorn|Essendon|Geelong|"
    r"Melbourne|North Melbourne|Port Adelaide|Adelaide|Brisbane|"
    r"GWS|Giants|Sydney Swans|West Coast|Fremantle|Gold Coast|"
    r"Western Bulldogs|St Kilda|"
    r"Hawks|Blues|Tigers|Magpies|Bombers|Cats|Demons|"
    r"Kangaroos|Roos|Power|Crows|Lions|Swans|Eagles|Dockers|"
    r"Suns|Bulldogs|Saints|"
    r"grand final|premiership|trade period|draft night|"
    r"tribunal|salary cap|supercoach|fantasy footy"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT = "Tyson.Densley@afl.com.au"

SLOT_LOOKBACK_HOURS = {"Morning": 9, "Midday": 6, "Afternoon": 5, "Evening": 5}


# ---------------------------------------------------------------------------
# Time-slot helpers
# ---------------------------------------------------------------------------

def get_time_slot() -> str:
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
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime.fromtimestamp(_time.mktime(val), tz=timezone.utc)
            except Exception:
                pass
    return None


def fetch_rss_articles(feeds: list[tuple]) -> list[dict]:
    articles = []
    for source, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)
            for entry in feed.entries[:25]:
                author = ""
                if getattr(entry, "author_detail", None):
                    author = entry.author_detail.get("name", "")
                if not author:
                    author = getattr(entry, "author", "")
                if not author:
                    for t in getattr(entry, "tags", []):
                        if (t.get("scheme") or "").endswith("creator"):
                            author = t.get("term", "")
                            break

                raw_title = getattr(entry, "title", "").strip()
                if " - " in raw_title:
                    raw_title = raw_title.rsplit(" - ", 1)[0].strip()

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
    """Drop non-AFL and match-result content; prefer genuine-news articles."""
    kept, deprioritised = [], []
    for a in articles:
        text = f"{a['title']} {a['snippet']}"
        # Hard gate: must mention AFL, a club, or football to qualify
        if not _AFL_RE.search(text):
            continue
        if _EXCLUDE_RE.search(text):
            continue
        if _INCLUDE_RE.search(text):
            kept.append(a)
        else:
            deprioritised.append(a)
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

def _clean_author(raw: str) -> str:
    """Strip 'By / by' prefix and trim whitespace from any author string."""
    # [\s:]* (zero-or-more) handles both 'By Name' and 'ByName' (two child spans)
    return re.sub(r"(?i)^by[\s:]*", "", raw.strip()).strip()


def _resolve_google_news_url(url: str) -> str:
    """
    Attempt to follow a Google News redirect URL to the real article page.
    Google News uses JS redirects so requests only gets as far as the Google
    landing page, but that page sometimes has a canonical tag or a <c-wiz>
    data attribute pointing to the real URL.
    Returns the resolved URL, or the original if resolution fails.
    """
    if "news.google.com" not in url:
        return url
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if "news.google.com" not in resp.url:
            return resp.url
        soup = BeautifulSoup(resp.text, "lxml")
        # Canonical tag
        canon = soup.find("link", rel="canonical")
        if canon and "news.google.com" not in (canon.get("href") or ""):
            return canon["href"]
        # Meta refresh
        refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if refresh:
            m = re.search(r"url=(.+)", refresh.get("content", ""), re.I)
            if m and "news.google.com" not in m.group(1):
                return m.group(1).strip()
    except Exception:
        pass
    return url


def _scrape_afl_author(url: str) -> str:
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
                val = el.get("content") or el.get_text(separator=" ", strip=True)
                if val and 2 < len(val) < 80:
                    return _clean_author(val)
    except Exception:
        pass
    return ""


def _scrape_media_author(url: str) -> str:
    """Scrape author from an article page using a broad set of selectors."""
    resolved = _resolve_google_news_url(url)
    if "news.google.com" in resolved:
        return ""
    try:
        resp = requests.get(resolved, headers=HEADERS, timeout=8, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")
        # Priority 1: meta tags
        for attr in (
            {"name": "author"},
            {"property": "article:author"},
            {"name": "byl"},
        ):
            tag = soup.find("meta", attrs=attr)
            if tag:
                val = tag.get("content", "").strip()
                if val and 2 < len(val) < 80:
                    return _clean_author(val)
        # Priority 2: schema.org itemprop
        for sel in ("[itemprop='author'] [itemprop='name']", "[itemprop='author']"):
            el = soup.select_one(sel)
            if el:
                val = el.get_text(strip=True)
                if val and 2 < len(val) < 80:
                    return _clean_author(val)
        # Priority 3: semantic class names (narrower first to avoid noise)
        for sel in (
            "[class*='author-name']",
            "[class*='author__name']",
            "[class*='ArticleAuthor']",
            "[class*='author']",
            "[class*='byline']",
            "[rel='author']",
        ):
            el = soup.select_one(sel)
            if el:
                val = el.get_text(strip=True)
                # Reject if too long (likely a "By John Smith and Jane Doe | Title" sentence)
                if val and 2 < len(val) < 60:
                    return _clean_author(val)
        # Priority 4: JSON-LD
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                author = data.get("author", {})
                if isinstance(author, list):
                    author = author[0]
                name = (author or {}).get("name", "")
                if name and 2 < len(name) < 80:
                    return _clean_author(name)
            except Exception:
                pass
    except Exception:
        pass
    return ""


def _scrape_og_image(url: str) -> str:
    """Scrape OG/Twitter image from url, attempting to resolve Google News redirects."""
    resolved = _resolve_google_news_url(url)
    target = resolved if "news.google.com" not in resolved else url
    try:
        resp = requests.get(target, headers=HEADERS, timeout=8, allow_redirects=True)
        if "news.google.com" in resp.url:
            return ""
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
    targets = [
        a for a in articles
        if not a.get("thumbnail") and a.get("link")
    ]
    if not targets:
        return articles

    print(f"      Enriching thumbnails for {len(targets)} articles...")
    url_to_thumb: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_scrape_og_image, a["link"]): a["link"]
                   for a in targets[:15]}
        for fut in as_completed(futures, timeout=25):
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
    """Scrape authors for AFL.com.au articles missing an author."""
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


def enrich_media_authors(articles: list[dict]) -> list[dict]:
    """Scrape authors for Other Media articles missing an author."""
    targets = [
        a for a in articles
        if a["source"] != "AFL.com.au" and not a["author"] and a.get("link")
    ]
    if not targets:
        return articles

    print(f"      Enriching media authors for {len(targets)} articles...")
    url_to_author: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_scrape_media_author, a["link"]): a["link"]
                   for a in targets[:12]}
        for fut in as_completed(futures, timeout=25):
            url = futures[fut]
            try:
                author = fut.result()
                if author:
                    url_to_author[url] = author
            except Exception:
                pass

    found = 0
    for a in articles:
        if a["link"] in url_to_author:
            a["author"] = url_to_author[a["link"]]
            found += 1
    print(f"      Found {found} media authors")
    return articles


# ---------------------------------------------------------------------------
# News card rendering
# ---------------------------------------------------------------------------

def _age_str(published: datetime | None) -> str:
    """Return a short human-readable age string, e.g. '3h ago'."""
    if not published:
        return ""
    age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
    if age_hours < 1:
        return f"{int(age_hours * 60)}m ago"
    if age_hours < 24:
        return f"{int(age_hours)}h ago"
    return f"{int(age_hours / 24)}d ago"


def render_news_card(article: dict, bold_title: bool = True) -> str:
    url    = article["link"]
    source = article["source"]
    author = article.get("author", "")
    thumb  = article.get("thumbnail", "")
    title  = article["title"]
    age    = _age_str(article.get("published"))

    byline = f'<span style="font-weight:600;">{source}</span>'
    if author:
        byline += f' &middot; <span style="font-style:italic;">{author}</span>'
    if age:
        byline += f' &middot; <span style="color:#aaaaaa;">{age}</span>'

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
# Article selection — keyword + recency heuristic
# ---------------------------------------------------------------------------

_PROMO_RE = re.compile(
    r"^(LISTEN|WATCH|STREAM|SUBSCRIBE|DOWNLOAD|WIN|ENTER|VOTE|POLL|QUIZ)[\s:–—]",
    re.IGNORECASE,
)


def _score_article(article: dict, now: datetime) -> float:
    text  = f"{article['title']} {article['snippet']}"
    score = 0.0
    if _INCLUDE_RE.search(text):
        score += 5.0
    if _EXCLUDE_RE.search(text):
        score -= 8.0
    # Promotional / service content (Stream your team, Listen:, Watch:, etc.)
    if _PROMO_RE.search(article["title"]):
        score -= 6.0
    if article.get("published"):
        age_hours = (now - article["published"]).total_seconds() / 3600
        score += max(0.0, 8.0 - age_hours * 0.4)
    return score


def select_afl_official(articles: list[dict]) -> str:
    if not articles:
        return "<p>No AFL.com.au news found this period.</p>"
    now    = datetime.now(timezone.utc)
    scored = sorted(articles, key=lambda a: _score_article(a, now), reverse=True)
    top    = scored[:5]
    print(f"  AFL.com.au top {len(top)}: " + " | ".join(a["title"][:40] for a in top))
    return "\n".join(render_news_card(a, bold_title=True) for a in top)


def select_media_news(articles: list[dict]) -> str:
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
    print(f"  Other media: {len(selected)} selected from {len(articles)}")
    return "\n".join(render_news_card(a, bold_title=False) for a in selected)


# ---------------------------------------------------------------------------
# Match results — articles we normally filter out, shown separately
# ---------------------------------------------------------------------------

def fetch_match_results(articles: list[dict], hours: int = 18) -> list[dict]:
    """
    Extract recent match-result articles from already-fetched AFL.com.au data.
    These are the items _EXCLUDE_RE catches — surfaced here instead of dropped.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    results = []
    seen: set[str] = set()
    for a in articles:
        text = f"{a['title']} {a['snippet']}"
        if not _EXCLUDE_RE.search(text):
            continue
        if not a.get("published") or a["published"] < cutoff:
            continue
        key = a["title"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        results.append(a)
    results.sort(key=lambda a: a["published"], reverse=True)
    print(f"  Match results: {len(results)} recent (last {hours}h)")
    return results[:6]


def render_results_section(results: list[dict]) -> str | None:
    if not results:
        return None
    items = []
    for r in results:
        age = _age_str(r.get("published"))
        age_html = f' &middot; <span style="color:#aaaaaa;">{age}</span>' if age else ""
        items.append(
            f'<div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #eeeeee;">'
            f'<a href="{r["link"]}" style="font-size:14px;color:#003087;text-decoration:none;'
            f'line-height:1.4;display:block;">{r["title"]}</a>'
            f'<p style="margin:4px 0 0 0;font-size:12px;color:#888888;">'
            f'<span style="font-weight:600;">{r["source"]}</span>{age_html}</p>'
            f'</div>'
        )
    return "\n".join(items)


# ---------------------------------------------------------------------------
# YouTube — AFL.com.au channel RSS
# ---------------------------------------------------------------------------

# Short clips and social-only content are usually identifiable by all-emoji
# titles or very short title length — exclude them.
_YT_SKIP_RE = re.compile(r"^[^\w]{0,5}$|#afl\w|^\W+$", re.IGNORECASE)


def fetch_youtube_videos(hours: int = 24) -> list[dict]:
    """Fetch recent videos from the AFL.com.au YouTube channel via RSS."""
    try:
        feed = feedparser.parse(AFL_YOUTUBE_RSS)
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        videos = []
        for entry in feed.entries[:15]:
            title = getattr(entry, "title", "").strip()
            if not title or _YT_SKIP_RE.search(title):
                continue
            pub = _parse_entry_date(entry)
            if pub and pub < cutoff:
                continue
            videos.append({
                "title":     title,
                "url":       getattr(entry, "link", ""),
                "published": pub,
            })
            if len(videos) == 5:
                break
        print(f"  YouTube: {len(videos)} recent videos")
        return videos
    except Exception as exc:
        print(f"  YouTube warning: {exc}")
        return []


def render_youtube_section(videos: list[dict]) -> str | None:
    if not videos:
        return None
    items = []
    for v in videos:
        age     = _age_str(v.get("published"))
        age_html = f' &middot; <span style="color:#aaaaaa;">{age}</span>' if age else ""
        items.append(
            f'<div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #eeeeee;">'
            f'<a href="{v["url"]}" style="font-size:14px;color:#cc0000;font-weight:bold;'
            f'text-decoration:none;line-height:1.4;display:block;">'
            f'&#9654; {v["title"]}</a>'
            f'<p style="margin:4px 0 0 0;font-size:12px;color:#888888;">'
            f'AFL YouTube{age_html}</p>'
            f'</div>'
        )
    return "\n".join(items)


# ---------------------------------------------------------------------------
# Fan Forums — Reddit + BigFooty
# ---------------------------------------------------------------------------

def _fetch_reddit_top_comment(thread_url: str) -> str | None:
    """Fetch the top comment from a Reddit thread URL."""
    try:
        m = re.search(r"/comments/([a-z0-9]+)/", thread_url)
        if not m:
            return None
        post_id = m.group(1)
        url  = f"https://www.reddit.com/r/AFL/comments/{post_id}.json?limit=5&sort=top"
        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=10)
        resp.raise_for_status()
        data     = resp.json()
        comments = data[1]["data"]["children"]
        for c in comments:
            body = c["data"].get("body", "").strip()
            if body and len(body) > 20 and body not in ("[deleted]", "[removed]"):
                if len(body) > 220:
                    body = body[:218] + "…"
                return body
    except Exception:
        pass
    return None


def fetch_reddit_threads() -> list[dict]:
    """
    Fetch r/AFL hot threads. Tries JSON API first, then RSS fallback.
    Also fetches the top comment for the #1 thread.
    """
    threads = []

    for base in ("https://old.reddit.com", "https://www.reddit.com"):
        url = f"{base}/r/AFL/hot.json?limit=15"
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
            resp.raise_for_status()
            for post in resp.json()["data"]["children"]:
                p = post["data"]
                if p.get("stickied"):
                    continue
                threads.append({
                    "title":       p.get("title", ""),
                    "comments":    p.get("num_comments", 0),
                    "score":       p.get("score", 0),
                    "flair":       p.get("link_flair_text", "") or "",
                    "url":         f"https://www.reddit.com{p.get('permalink', '')}",
                    "top_comment": None,
                })
                if len(threads) == 5:
                    break
            if threads:
                print(f"      Reddit: JSON API succeeded ({base})")
                break
        except Exception as exc:
            print(f"  Reddit JSON warning ({base}): {exc}")

    if not threads:
        for rss_base in ("https://www.reddit.com", "https://old.reddit.com"):
            rss_url = f"{rss_base}/r/AFL/hot.rss"
            try:
                feed = feedparser.parse(rss_url, request_headers=REDDIT_HEADERS)
                if not feed.entries:
                    continue
                for entry in feed.entries[:10]:
                    title = re.sub(r"^r/AFL:\s*", "", getattr(entry, "title", "").strip())
                    url   = getattr(entry, "link", "").strip()
                    if title and url:
                        threads.append({
                            "title": title, "comments": None, "score": None,
                            "flair": "", "url": url, "top_comment": None,
                        })
                    if len(threads) == 5:
                        break
                if threads:
                    print(f"      Reddit: RSS fallback succeeded ({rss_base})")
                    break
            except Exception as exc:
                print(f"  Reddit RSS warning ({rss_base}): {exc}")

    # Fetch top comment for the #1 thread (most engaging)
    if threads and threads[0].get("comments"):
        threads[0]["top_comment"] = _fetch_reddit_top_comment(threads[0]["url"])
        if threads[0]["top_comment"]:
            print("      Reddit: top comment fetched for #1 thread")

    if not threads:
        print("  Reddit: all methods failed")
    return threads


def fetch_bigfooty_threads() -> list[dict]:
    """Scrape hot threads from BigFooty AFL forum."""
    try:
        resp = requests.get(BIGFOOTY_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "lxml")
        threads = []
        seen: set[str] = set()
        # XenForo thread links follow /forum/threads/title.ID/ pattern
        for a in soup.find_all("a", href=re.compile(r"/forum/threads/")):
            href  = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or len(title) < 12:
                continue
            # Deduplicate by thread ID (last numeric segment)
            tid = re.search(r"\.(\d+)/?$", href)
            key = tid.group(1) if tid else href
            if key in seen:
                continue
            seen.add(key)
            url = f"https://www.bigfooty.com{href}" if href.startswith("/") else href
            threads.append({"title": title, "url": url})
            if len(threads) == 5:
                break
        print(f"  BigFooty: {len(threads)} threads")
        return threads
    except Exception as exc:
        print(f"  BigFooty warning: {exc}")
        return []


def render_forum_section(reddit: list[dict], bigfooty: list[dict]) -> str:
    """Render Reddit and BigFooty threads combined into one Fan Forums section."""
    parts = []

    if reddit:
        parts.append(
            '<p style="margin:0 0 10px 0;font-size:11px;font-weight:bold;color:#ff4500;'
            'text-transform:uppercase;letter-spacing:0.08em;">r/AFL</p>'
        )
        for t in reddit:
            flair_html = ""
            if t.get("flair"):
                flair_html = (
                    f' <span style="font-size:11px;color:#ffffff;background:#003087;'
                    f'border-radius:3px;padding:1px 5px;vertical-align:middle;">'
                    f'{t["flair"]}</span>'
                )
            meta = "r/AFL"
            if t.get("comments") is not None:
                meta += f' &middot; {t["comments"]:,} comments &middot; {t["score"]:,} upvotes'

            top_comment_html = ""
            if t.get("top_comment"):
                top_comment_html = (
                    f'<p style="margin:8px 0 0 0;font-size:13px;color:#555555;line-height:1.5;'
                    f'border-left:3px solid #ff4500;padding-left:10px;font-style:italic;">'
                    f'&ldquo;{t["top_comment"]}&rdquo;</p>'
                )

            parts.append(
                f'<div style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #eeeeee;">'
                f'<a href="{t["url"]}" style="font-size:14px;font-weight:bold;color:#003087;'
                f'text-decoration:none;line-height:1.4;display:block;">'
                f'{t["title"]}</a>{flair_html}'
                f'<p style="margin:5px 0 0 0;font-size:12px;color:#888888;">{meta}</p>'
                f'{top_comment_html}'
                f'</div>'
            )

    if bigfooty:
        parts.append(
            '<p style="margin:18px 0 10px 0;font-size:11px;font-weight:bold;color:#1a3c6e;'
            'text-transform:uppercase;letter-spacing:0.08em;">BigFooty</p>'
        )
        for t in bigfooty:
            parts.append(
                f'<div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #eeeeee;">'
                f'<a href="{t["url"]}" style="font-size:14px;font-weight:bold;color:#003087;'
                f'text-decoration:none;line-height:1.4;display:block;">{t["title"]}</a>'
                f'<p style="margin:4px 0 0 0;font-size:12px;color:#888888;">BigFooty</p>'
                f'</div>'
            )

    if not parts:
        return "<p style='font-size:14px;color:#555;'>No forum threads available this period.</p>"

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

def _section_row(heading: str, content: str, first: bool = False) -> str:
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
    afl_html:     str,
    media_html:   str,
    results_html: str | None,
    youtube_html: str | None,
    forum_html:   str,
    time_slot:    str,
    now_aest:     datetime,
) -> str:
    day_date  = now_aest.strftime("%A %-d %B %Y")
    generated = now_aest.strftime("%-I:%M %p AEST")

    results_section = _section_row("Match Results", results_html) if results_html else ""
    youtube_section = _section_row("Watch", youtube_html)        if youtube_html else ""

    body_sections = (
        _section_row("Top Stories — AFL.com.au", afl_html, first=True)
        + _section_row("Top Stories — Other Media", media_html)
        + results_section
        + youtube_section
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

SCRIPT_VERSION = "v25"


def main() -> None:
    now_aest  = datetime.now(AEST)
    time_slot = get_time_slot()
    hours_lookback = SLOT_LOOKBACK_HOURS.get(time_slot, 6)

    day_date = now_aest.strftime("%A %-d %B")
    print(f"\n=== AFL Digest {SCRIPT_VERSION} — {time_slot} — {day_date} (lookback {hours_lookback}h) ===\n")

    # ── Section 1a: AFL.com.au ──
    print("[1/5] Fetching AFL.com.au feed...")
    afl_raw  = fetch_rss_articles(AFL_OFFICIAL_FEEDS)
    afl_pool = filter_by_recency(afl_raw, "AFL.com.au", hours=hours_lookback, min_recent=3)
    afl_pool = enrich_authors(afl_pool)
    print(f"      {len(afl_raw)} fetched → {len(afl_pool)} in pool")
    afl_html = select_afl_official(afl_pool)

    # Match results captured from raw feed (articles excluded from news sections)
    results     = fetch_match_results(afl_raw, hours=hours_lookback + 9)
    results_html = render_results_section(results)

    # ── Section 1b: Other Media ──
    print("[2/5] Fetching other media feeds...")
    media_raw      = fetch_rss_articles(OTHER_MEDIA_FEEDS)
    media_filtered = filter_articles(media_raw)
    media_recent   = filter_by_recency(media_filtered, "media")
    media_recent   = enrich_thumbnails(media_recent)
    media_recent   = enrich_media_authors(media_recent)
    print(f"      {len(media_raw)} fetched → {len(media_filtered)} filtered → {len(media_recent)} recent")
    media_html = select_media_news(media_recent)

    # ── YouTube ──
    print("[3/5] Fetching AFL YouTube videos...")
    youtube_html = render_youtube_section(fetch_youtube_videos(hours=hours_lookback + 9))

    # ── Fan Forums: Reddit + BigFooty ──
    print("[4/5] Fetching forum threads...")
    reddit   = fetch_reddit_threads()
    bigfooty = fetch_bigfooty_threads()
    print(f"      Reddit: {len(reddit)} threads | BigFooty: {len(bigfooty)} threads")
    forum_html = render_forum_section(reddit, bigfooty)

    # ── Email ──
    print("[5/5] Sending email...")
    date_str  = now_aest.strftime("%-d %B")
    now_utc   = datetime.now(timezone.utc)
    all_articles = afl_pool + media_recent
    top = max(all_articles, key=lambda a: _score_article(a, now_utc), default=None)
    if top:
        headline = top["title"]
        if len(headline) > 65:
            headline = headline[:64].rstrip() + "…"
        subject = f"{headline} | {time_slot} {date_str}"
    else:
        subject = f"{time_slot} - {now_aest.strftime('%A')} {date_str}"

    html = build_email_html(
        afl_html, media_html, results_html, youtube_html, forum_html, time_slot, now_aest
    )
    print(f"      Subject: {subject}")
    send_email(html, subject)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
