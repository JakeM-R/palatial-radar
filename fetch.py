"""Palatial physical-AI radar: pull live feeds, sort with keyword rules, write site/data.json.

Runs daily from .github/workflows/refresh.yml. Real data only, no model calls.
Production version would replace RULES with an LLM pass that summarises each item
and scores its relevance; the rules here are deliberately simple and every item
records which rule matched so a reader can see why it is on the page.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "site", "data.json")
NOW = datetime.now(timezone.utc)
LOOKBACK_DAYS = 63          # 9 weeks: this week + 8-week baseline
KEEP_DAYS = 120             # store keeps items this long so shallow RSS feeds build history
UA = "palatial-radar (github.com/JakeM-R/palatial-radar)"
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ---------- sources ----------
# Simulators Palatial exports to (per palatial.cloud), plus Genesis.
RELEASE_REPOS = {
    "isaac-sim/IsaacSim": "Isaac Sim",
    "isaac-sim/IsaacLab": "Isaac Lab",
    "google-deepmind/mujoco": "MuJoCo",
    "newton-physics/newton": "Newton",
    "PixarAnimationStudios/OpenUSD": "OpenUSD",
    "Genesis-Embodied-AI/Genesis": "Genesis",
}
NEWS_RSS = {
    "The Robot Report": "https://www.therobotreport.com/feed/",
    "TechCrunch Robotics": "https://techcrunch.com/category/robotics/feed/",
}
FEDREG_TERMS = [
    "robotics", "robot", "humanoid", "industrial machinery section 232",
    "artificial intelligence export controls", "advanced computing export",
    "autonomous systems",
]
HN_QUERIES = ["robotics", "humanoid", "physical AI", "robot simulation", "sim-to-real", "world model robot"]
ARXIV_QUERY = ('cat:cs.RO AND (ti:"sim-to-real" OR ti:"real-to-sim" OR ti:"digital twin" '
               'OR ti:"simulation-ready" OR ti:"sim-ready" OR ti:"articulated object" '
               'OR ti:"asset generation" OR ti:"scene generation")')

# Sources with a full 9-week history on every run. Only these feed the baseline,
# so a shallow RSS feed can't make "this week" look busier than it is.
DEEP_SOURCES = {"Federal Register", "GitHub", "Hacker News", "arXiv"}

# ---------- rules ----------
RELEVANT = re.compile(
    r"\b(robot\w*|humanoid\w*|physical ai|embodied|sim[- ]to[- ]real|real[- ]to[- ]sim|digital twin\w*|"
    r"simulat\w*|world model\w*|isaac|mujoco|omniverse|openusd|autonomous|manipulat\w*|teleoperat\w*|"
    r"industrial machinery|warehouse automation)\b", re.I)
RULES = [
    # (category, compiled regex, label shown on page)
    ("Funding", re.compile(r"\b(raises?|raised|funding|series [a-f]\b|seed round|pre-seed|"
                           r"\$\s?\d+(\.\d+)?\s?(m|mn|million|b|bn|billion)\b|valuation|valued at|worth \$\S+|acquir\w+|acquisition|ipo|spac)\b", re.I),
     "funding / deal terms"),
    ("Policy", re.compile(r"\b(tariff\w*|export control\w*|regulat\w*|ruling|section 232|commerce department|"
                          r"bureau of industry|nist|ai act|standard\w*|executive order|legislation|bill|ban)\b", re.I),
     "policy terms"),
    ("Launch", re.compile(r"\b(launch\w*|unveil\w*|introduc\w+|announc\w+|debut\w*|open[- ]sourc\w+|"
                          r"releases?|now available|ships?|partners? with|partnership|emerges? from stealth|starts? production)\b", re.I),
     "launch terms"),
]


def get(url, headers=None, tries=4):
    h = {"User-Agent": UA}
    h.update(headers or {})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                print(f"  ! failed {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def make(source, outlet, title, url, when, category=None, rule=None, kind="news"):
    return {"id": hashlib.sha1(url.encode()).hexdigest()[:12], "source": source, "outlet": outlet,
            "title": clean(title)[:220], "url": url, "date": iso(when), "category": category,
            "rule": rule, "kind": kind}


def classify(item, text):
    """Apply keyword rules. Fixed categories (Policy for Federal Register, Release for
    GitHub, Research for arXiv) are set by the source and not overridden."""
    if item["category"]:
        return item
    for cat, rx, label in RULES:
        m = rx.search(text)
        if m:
            item["category"], item["rule"] = cat, f'{label}: "{m.group(0).strip()}"'
            return item
    item["category"], item["rule"] = "Market", "robotics terms, no funding/policy/launch match"
    return item


# ---------- fetchers ----------
def fetch_releases(since):
    out = []
    hdr = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        hdr["Authorization"] = f"Bearer {GH_TOKEN}"
    for repo, name in RELEASE_REPOS.items():
        raw = get(f"https://api.github.com/repos/{repo}/releases?per_page=30", hdr)
        if not raw:
            continue
        for r in json.loads(raw):
            if r.get("draft") or not r.get("published_at"):
                continue
            when = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            if when < since:
                continue
            tag = r.get("tag_name", "")
            pre = r.get("prerelease") or bool(re.search(r"(rc|alpha|beta|a\d|b\d)", tag, re.I))
            title = f'{name} {tag}' + (" (pre-release)" if pre else "")
            body = r.get("body") or ""
            breaking = bool(re.search(r"breaking change|\bBREAKING\b|backwards?[- ]incompatible", body))
            rule = "GitHub release" + (" · notes flag breaking changes" if breaking else "")
            it = make("GitHub", name, title, r["html_url"], when, "Release", rule, "release")
            it["flag"] = "breaking" if breaking else ("pre" if pre else None)
            out.append(it)
    return out


def fetch_fedreg(since):
    out, seen = [], set()
    for term in FEDREG_TERMS:
        q = [("conditions[term]", term), ("conditions[publication_date][gte]", since.strftime("%Y-%m-%d")),
             ("order", "newest"), ("per_page", "100")]
        q += [("fields[]", f) for f in ("title", "html_url", "publication_date", "type", "agencies", "abstract")]
        url = "https://www.federalregister.gov/api/v1/documents.json?" + urllib.parse.urlencode(q)
        raw = get(url)
        if not raw:
            continue
        for d in json.loads(raw).get("results", []):
            if d["html_url"] in seen:
                continue
            text = f'{d.get("title","")} {d.get("abstract") or ""}'
            if not RELEVANT.search(text):
                continue  # full-text hits where robotics is incidental
            seen.add(d["html_url"])
            when = datetime.fromisoformat(d["publication_date"]).replace(tzinfo=timezone.utc)
            agency = ", ".join(a.get("name", "") for a in (d.get("agencies") or []) if a.get("name"))[:80]
            it = make("Federal Register", agency or "Federal Register", d["title"], d["html_url"], when,
                      "Policy", f'Federal Register {d.get("type","document").lower()} · search "{term}"', "policy")
            out.append(it)
    return out


def parse_rss(raw, outlet):
    out = []
    root = ET.fromstring(raw)
    for el in root.iter("item"):
        title = el.findtext("title") or ""
        link = (el.findtext("link") or "").strip()
        pub = el.findtext("pubDate")
        desc = el.findtext("description") or ""
        if not (title and link and pub):
            continue
        try:
            when = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        out.append((title, link, when, desc))
    return out


def fetch_news(since):
    out = []
    for outlet, url in NEWS_RSS.items():
        raw = get(url)
        if not raw:
            continue
        try:
            rows = parse_rss(raw, outlet)
        except ET.ParseError as e:
            print(f"  ! bad RSS {outlet}: {e}", file=sys.stderr)
            continue
        for title, link, when, desc in rows:
            if when < since:
                continue
            text = f"{title} {clean(desc)}"
            if not RELEVANT.search(text):
                continue
            # Classify on the headline only: descriptions are long and trip every rule.
            out.append(classify(make("News", outlet, title, link, when), title))
    return out


def fetch_hn(since):
    out, seen = [], set()
    for q in HN_QUERIES:
        url = ("https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode({
            "query": q, "tags": "story", "hitsPerPage": "200",
            "numericFilters": f"created_at_i>{int(since.timestamp())},points>=15"}))
        raw = get(url)
        if not raw:
            continue
        for h in json.loads(raw).get("hits", []):
            link = h.get("url") or f'https://news.ycombinator.com/item?id={h["objectID"]}'
            title = h.get("title") or ""
            if link in seen or not RELEVANT.search(title):
                continue
            seen.add(link)
            when = datetime.fromtimestamp(h["created_at_i"], timezone.utc)
            domain = urllib.parse.urlparse(link).netloc.replace("www.", "")
            it = classify(make("Hacker News", domain or "Hacker News", title, link, when), title)
            it["points"] = h.get("points")
            out.append(it)
    return out


def fetch_arxiv(since):
    url = ("http://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": ARXIV_QUERY, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": "150"}))
    raw = get(url)
    if not raw:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(raw).findall("a:entry", ns):
        when = datetime.fromisoformat(e.findtext("a:published", "", ns).replace("Z", "+00:00"))
        if when < since:
            continue
        link = e.findtext("a:id", "", ns).strip()
        title = e.findtext("a:title", "", ns)
        hit = next((t for t in re.findall(r'ti:"([^"]+)"', ARXIV_QUERY) if t.lower() in title.lower()), "title term")
        out.append(make("arXiv", "arXiv cs.RO", title, link, when, "Research", f'arXiv cs.RO title: "{hit}"', "research"))
    return out


# ---------- baseline ----------
def weekly(items):
    """Counts per category for week 0 (last 7 days) and weeks 1-8 before it, deep sources only."""
    cats = ["Policy", "Release", "Funding", "Launch", "Research", "Market"]
    counts = {c: [0] * 9 for c in cats}
    for it in items:
        if it["source"] not in DEEP_SOURCES or it["category"] not in counts:
            continue
        age = (NOW - datetime.fromisoformat(it["date"].replace("Z", "+00:00"))).days
        w = age // 7
        if 0 <= w < 9:
            counts[it["category"]][w] += 1
    return counts


def main():
    since = NOW - timedelta(days=LOOKBACK_DAYS)
    fresh, status = [], {}
    for name, fn in [("GitHub releases", fetch_releases), ("Federal Register", fetch_fedreg),
                     ("News RSS", fetch_news), ("Hacker News", fetch_hn), ("arXiv", fetch_arxiv)]:
        try:
            got = fn(since)
        except Exception as e:  # noqa: BLE001  one bad source shouldn't sink the run
            print(f"  ! {name} crashed: {e}", file=sys.stderr)
            got = []
        status[name] = len(got)
        print(f"{name}: {len(got)}")
        fresh += got

    # Merge with the stored items so shallow feeds accumulate history.
    old = []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT)).get("items", [])
        except (ValueError, OSError):
            old = []
    keep_after = NOW - timedelta(days=KEEP_DAYS)
    by_id = {it["id"]: it for it in old
             if datetime.fromisoformat(it["date"].replace("Z", "+00:00")) >= keep_after}
    by_id.update({it["id"]: it for it in fresh})
    items = sorted(by_id.values(), key=lambda x: x["date"], reverse=True)

    data = {"generated": iso(NOW), "sources": status, "weekly": weekly(items),
            "deep_sources": sorted(DEEP_SOURCES), "release_repos": RELEASE_REPOS,
            "fedreg_terms": FEDREG_TERMS, "items": items}
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    print(f"wrote {len(items)} items")


if __name__ == "__main__":
    main()
