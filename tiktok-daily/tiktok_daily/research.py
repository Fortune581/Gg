import concurrent.futures
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .common import WorkflowError, normalise, request, topic_key

HT = "{https://trends.google.com/trending/rss}"


def primary_source(url, cfg):
    """Recognize first-party publishers by HTTPS host, never by their display name."""
    parts = urlsplit(url or "")
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    host = (parts.hostname or "").removeprefix("www.")
    trusted = {(urlsplit(f["url"]).hostname or "").removeprefix("www.")
               for f in cfg["news_feeds"] if f.get("primary")}
    return any(host == domain or host.endswith("." + domain) for domain in trusted if domain)


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def date_value(value):
    try:
        result = parsedate_to_datetime(value)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_fresh(value, now, max_hours):
    date = date_value(value)
    if date is None:
        return False
    hours = (now - date).total_seconds() / 3600
    return -0.25 <= hours <= max_hours


def eligible(text, cfg):
    text = normalise(text)
    def includes(word):
        return re.search(r"\b" + re.escape(word) + r"\b", text) is not None
    return any(includes(w) for w in cfg["allowed_topic_words"]) and not any(includes(w) for w in cfg["excluded_topic_words"])


def parse_trends(xml, geo, cfg, now):
    result = []
    for item in ET.fromstring(xml).findall("./channel/item"):
        title = clean(item.findtext("title"))
        published = item.findtext("pubDate")
        sources = [{"title": clean(n.findtext(HT + "news_item_title")),
                    "url": n.findtext(HT + "news_item_url"),
                    "publisher": clean(n.findtext(HT + "news_item_source")),
                    "primary": primary_source(n.findtext(HT + "news_item_url"), cfg)}
                   for n in item.findall(HT + "news_item")]
        if not is_fresh(published, now, cfg["max_age_hours"]) or not eligible(title + " " + " ".join(s["title"] for s in sources), cfg):
            continue
        result.append({"title": title, "published": published, "region": geo,
                       "signal": f"Google search trend ({geo}); not TikTok ranking",
                       "signal_label": "SEARCH TREND / " + geo,
                       "traffic": clean(item.findtext(HT + "approx_traffic")),
                       "sources": sources[:3], "key": topic_key(title)})
    return result


def parse_news(xml, feed, cfg, now):
    result = []
    for item in ET.fromstring(xml).findall("./channel/item"):
        title = clean(item.findtext("title"))
        published = item.findtext("pubDate")
        context = title + " " + " ".join(feed.get("topics", []))
        if not is_fresh(published, now, cfg["max_age_hours"]) or not eligible(context, cfg):
            continue
        summary = clean(item.findtext("description"))
        result.append({"title": title, "published": published, "region": feed["region"],
                       "signal": "Fresh news feed; no measured TikTok or search ranking",
                       "signal_label": "IN THE NEWS", "traffic": None,
                       "sources": [{"title": title, "url": item.findtext("link"),
                                    "publisher": feed["name"], "primary": feed["primary"],
                                    "feed_summary": summary[:1200]}], "key": topic_key(title)})
    return result


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ignore = 0
        self.paragraph = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore += 1
        if tag == "p" and self.ignore == 0:
            self.paragraph = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore = max(0, self.ignore - 1)
        if tag == "p" and self.paragraph is not None:
            line = clean(" ".join(self.paragraph))
            if len(line.split()) >= 12 and not re.search(r"cookie|subscribe|sign up|privacy policy", line, re.I):
                self.text.append(line)
            self.paragraph = None

    def handle_data(self, data):
        if self.paragraph is not None and self.ignore == 0:
            self.paragraph.append(data)


def source_excerpt(source):
    url = source.get("url") or ""
    parts = urlsplit(url)
    # Sources are text-only research inputs, never executable instructions.
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise WorkflowError("Source must have a public HTTPS URL")
    parser = Paragraphs()
    fetch_error = None
    try:
        parser.feed(request(url, timeout=15).decode("utf-8", errors="replace"))
    except WorkflowError as exc:
        fetch_error = str(exc)
    text = " ".join(parser.text)
    origin = "article"
    # A summary already retrieved from the first-party RSS feed remains usable
    # when the linked article is unavailable. Never substitute third-party snippets.
    if len(text.split()) < 35 and source.get("primary"):
        text = source.get("feed_summary", "")
        origin = "primary_feed_summary"
    if len(text.split()) < 35:
        raise WorkflowError(fetch_error or "Source has insufficient accessible article text")
    return {**source, "excerpt": " ".join(text.split()[:280]), "domain": parts.hostname,
            "evidence_origin": origin}


def research(cfg, now, seen=()):
    feeds = [(f"https://trends.google.com/trending/rss?geo={geo}", "trend", geo) for geo in cfg["trend_geos"]]
    feeds += [(f["url"], "news", f) for f in cfg["news_feeds"]]
    diagnostics, candidates = [], []

    def collect(entry):
        url, kind, meta = entry
        try:
            xml = request(url, timeout=15)
            items = parse_trends(xml, meta, cfg, now) if kind == "trend" else parse_news(xml, meta, cfg, now)
            return items, {"url": url, "ok": True, "candidates": len(items)}
        except (WorkflowError, ET.ParseError) as exc:
            return [], {"url": url, "ok": False, "reason": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for items, diagnostic in pool.map(collect, feeds):
            candidates.extend(items)
            diagnostics.append(diagnostic)
    unique = {c["key"]: c for c in candidates if c["key"] not in seen}
    prefer_zambia = now.day % 2 == 0
    candidates = sorted(unique.values(), key=lambda c: (
        c["region"] == "ZM" if prefer_zambia else c["region"] != "ZM",
        c["traffic"] is not None,
        any(s.get("primary") for s in c["sources"]),
        date_value(c["published"])), reverse=True)
    return candidates[:16], diagnostics


def enrich(candidate):
    verified = []
    rejected = []
    for source in sorted(candidate["sources"], key=lambda s: bool(s.get("primary")), reverse=True):
        try:
            extracted=source_excerpt(source)
            topic_words={w for w in normalise(candidate["title"]).split() if len(w)>=4}
            excerpt_words=set(normalise(extracted["excerpt"]).split())
            if topic_words and len(topic_words & excerpt_words)<min(2,len(topic_words)):
                rejected.append({"url": source["url"], "reason": "Extracted text did not match the topic"})
                continue
            verified.append(extracted)
            if extracted.get("primary"):
                # Prefer the originating publisher's account over looser secondary
                # headlines. One accessible first-party source satisfies this policy.
                verified = [extracted]
                break
        except WorkflowError as exc:
            rejected.append({"url": source.get("url"), "reason": str(exc)})
    primary = any(s.get("primary") for s in verified)
    domains = {s["domain"].removeprefix("www.") for s in verified}
    if not primary and len(domains) < 2:
        details = "; ".join(r["reason"] for r in rejected)
        raise WorkflowError("Need one primary publisher or two accessible publisher domains"
                            + (": " + details if details else ""))
    for i, source in enumerate(verified):
        source["id"] = str(i + 1)
    return {**candidate, "sources": verified}
