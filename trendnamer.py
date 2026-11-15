#!/usr/bin/env python3
"""
trendnamer — find available, brandable domain names in what people are talking about right now.

Pipeline:  trend sources -> phrase extraction -> velocity (vs. previous runs) -> trademark/entity risk
           -> domain candidates -> RDAP availability + TLD pricing -> ranked CSV + HTML report

Usage:
  python trendnamer.py                       # live: pull sources, check availability, write report
  python trendnamer.py --snapshot demo.json  # run on a saved snapshot instead of live sources
  python trendnamer.py --no-check            # skip availability lookups (fast)
  python trendnamer.py --save-snapshot s.json  # keep today's raw signals for later replay

Only uses the Python standard library. Sources need no API keys.
"""
import argparse, base64, csv, datetime as dt, html, json, math, os, re, socket, sys, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

UA = "trendnamer/0.1 (prototype; domain-name research)"
HERE = Path(__file__).resolve().parent
HISTORY = HERE / "history.json"

def load_dotenv(*paths):
    """Read KEY=value lines from .env files. Values already set in the environment win."""
    for path in paths:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip().strip("'\"")
            os.environ.setdefault(k, v)

load_dotenv(Path.cwd() / ".env", HERE / ".env")

# ---------------------------------------------------------------- sources
def _get(url, data=None, timeout=12, headers=None):
    h = {"User-Agent": UA, "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def src_google_trends(geo="US"):
    """Google Trends daily trending searches (RSS). These are mostly named entities."""
    _, body = _get(f"https://trends.google.com/trending/rss?geo={geo}")
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for item in ET.fromstring(body).iter("item"):
        title = item.findtext("title") or ""
        traffic = item.findtext("ht:approx_traffic", default="1000", namespaces=ns)
        w = int(re.sub(r"\D", "", traffic) or 1000)
        out.append({"text": title, "source": "google_trends", "weight": w, "entity": True})
    return out

def src_wikipedia(days_back=1):
    """Most-viewed English Wikipedia articles yesterday, with the same day a week earlier as prior."""
    def top(day):
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/"
               f"{day:%Y/%m/%d}")
        _, body = _get(url)
        return {a["article"]: a["views"] for a in json.loads(body)["items"][0]["articles"]}
    today = dt.date.today() - dt.timedelta(days=days_back)
    now, prior = top(today), top(today - dt.timedelta(days=7))
    skip = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Talk:)")
    return [{"text": a.replace("_", " "), "source": "wikipedia", "weight": v,
             "prior_weight": prior.get(a, 0), "entity": True}
            for a, v in list(now.items())[:200] if not skip.match(a)]

def src_hackernews():
    _, body = _get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60")
    return [{"text": h["title"], "source": "hackernews", "weight": h.get("points", 1) or 1}
            for h in json.loads(body)["hits"]]

def src_reddit(sub="popular"):
    """Reddit blocks anonymous requests. Create a free 'script' app at reddit.com/prefs/apps and set
    REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to enable this source."""
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError("skipped: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (see README)")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    _, tok = _get("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                  headers={"Authorization": f"Basic {auth}",
                           "Content-Type": "application/x-www-form-urlencoded"})
    token = json.loads(tok)["access_token"]
    _, body = _get(f"https://oauth.reddit.com/r/{sub}/top?t=day&limit=100",
                   headers={"Authorization": f"Bearer {token}"})
    return [{"text": c["data"]["title"], "source": f"reddit/{sub}", "weight": c["data"].get("score", 1)}
            for c in json.loads(body)["data"]["children"]]

def src_google_news():
    """Google News top stories RSS: what the news is about (mostly entities, useful for overlap)."""
    _, body = _get("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en")
    items = list(ET.fromstring(body).iter("item"))
    return [{"text": re.sub(r"\s+-\s+[^-]+$", "", i.findtext("title") or ""), "source": "google_news",
             "weight": max(1, 100 - n)} for n, i in enumerate(items)]

def src_bluesky():
    """Bluesky's public trending topics. Good for slang and memes; no account needed."""
    _, body = _get("https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics?limit=25")
    d = json.loads(body)
    topics = d.get("topics", []) + d.get("suggested", [])
    return [{"text": t.get("displayName") or t.get("topic", ""), "source": "bluesky",
             "weight": max(1, 50 - n)} for n, t in enumerate(topics)]

SOURCES = {"google_trends": src_google_trends, "wikipedia": src_wikipedia, "hackernews": src_hackernews,
           "google_news": src_google_news, "bluesky": src_bluesky, "reddit": src_reddit}

def collect(names):
    signals = []
    for n in names:
        try:
            got = SOURCES[n]()
            print(f"  {n:14s} {len(got):4d} signals", file=sys.stderr)
            signals += got
        except Exception as e:
            print(f"  {n:14s} FAILED ({e.__class__.__name__}: {e})", file=sys.stderr)
    return signals

# ---------------------------------------------------------------- phrases
STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its
this that these those i you he she we they me my your our their his her them us not no yes do does did
how what why when where who which will would can could should may might just new now after before over
about into out up down off than then so very more most some any all one two three says said vs via
amid says first last year years day days week weeks time like get got make made our here there""".split())

def norm(text):
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')

def tokens(text):
    toks = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", norm(text).lower())
    return [t for t in toks if len(t) > 1 or t.isdigit()]

def raw_words(text):
    return re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", norm(text))

def is_title_case_headline(words):
    long = [w for w in words if len(w) > 3]
    return len(long) >= 3 and sum(w[0].isupper() for w in long) / len(long) > 0.6

GENERIC_ACRONYMS = set("ai ok diy irl lol omg tv pc vr ar ev uk us usa eu ceo api app ios gpu cpu nft"
                       " pov fyi tbh imo ftw goat npc dm dms".split())

def proper_tokens(signals):
    """Words usually written with a capital mid-sentence (Biden, SpaceX, FAA) are names, not phrases.
    Headlines In Title Case are skipped, since they capitalise everything."""
    cap, low = defaultdict(int), defaultdict(int)
    for s in signals:
        words = raw_words(s["text"])
        if len(words) < 3 or is_title_case_headline(words):
            continue
        for w in words[1:]:
            (cap if w[0].isupper() else low)[w.lower()] += 1
    return {w for w, c in cap.items() if c >= 1 and c / (c + low[w]) >= 0.6 and w not in GENERIC_ACRONYMS}

def is_titlecase_entity(text):
    """Heuristic: 2+ capitalized words in a row usually means a person, place, show or brand."""
    return bool(re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", text))

HEADLINE_VERBS = set("""says said blames blamed slams wins won loses lost gets got catches caught warns
calls called hits launches launched announces announced reveals revealed sues sued faces plans
claims denies reports urges vows backs bans signs kills dies dead arrested charged fired joins
leaves returns makes takes gives shows finds need needs really want wants""".split())

# Words that describe the format of a trend rather than its subject ("X challenge" -> "X").
FORMAT_WORDS = set("trend trends challenge meme memes prank debate reveal video tiktok viral song".split())

def core(toks):
    while toks and toks[-1] in FORMAT_WORDS:
        toks = toks[:-1]
    while toks and (toks[0] in STOP or toks[0] in {"best", "worst", "the"}):
        toks = toks[1:]
    return toks

def signal_grams(toks, max_n):
    grams = set()
    for n in range(1, max_n + 1):
        for i in range(len(toks) - n + 1):
            g = toks[i:i + n]
            if g[0] in STOP or g[-1] in STOP or (n > 1 and all(t.isdigit() for t in g)):
                continue
            if n > 1 and (g[0] in HEADLINE_VERBS or g[-1] in HEADLINE_VERBS):
                continue
            grams.add(" ".join(g))
    return grams

# Sources whose items are posts or headlines. Everything else (Google Trends, Wikipedia, Bluesky topics,
# hand-entered slang/TikTok lists) is a list of trend *terms*, which can be taken whole.
HEADLINE_SOURCES = ("hackernews", "google_news", "reddit")

def is_headline(s):
    return s.get("kind") == "headline" or (s.get("kind") != "term" and s["source"].startswith(HEADLINE_SOURCES))

def extract_phrases(signals, max_n=3, short=5):
    """
    Short signals (slang, trend names, search terms) count as whole phrases, minus format words.
    Long signals (headlines) only contribute n-grams that recur across 2+ different signals,
    so one headline can't turn its generic words into 'trends'.
    """
    parsed = [(s, tokens(s["text"])) for s in signals]
    recur, recur_src = defaultdict(set), defaultdict(set)
    for idx, (s, toks) in enumerate(parsed):
        for g in signal_grams(toks, max_n):
            recur[g].add(idx); recur_src[g].add(s["source"])
    proper = proper_tokens(signals)
    # Normalise weights within each source so 40k monthly searches and 50 TikTok creators compare fairly.
    top = defaultdict(lambda: 1.0)
    for s in signals:
        top[s["source"]] = max(top[s["source"]], math.log1p(s.get("weight", 1)))
    score, prior, srcs, entity, examples = (defaultdict(float), defaultdict(float),
                                            defaultdict(set), defaultdict(int), {})
    for idx, (s, toks) in enumerate(parsed):
        w = math.log1p(s.get("weight", 1)) / top[s["source"]]
        pw = (math.log1p(s.get("prior_weight", 0)) / top[s["source"]]) if "prior_weight" in s else None
        # From headlines, only multi-word phrases that recur; a lone word like "end" isn't a trend.
        grams = {g for g in signal_grams(toks, max_n)
                 if " " in g and (len(recur_src[g]) >= 2 or len(recur[g]) >= 3)}
        if len(toks) <= short and not is_headline(s):
            c = core(toks)
            if c:
                grams.add(" ".join(c))
        ent = s.get("entity") or is_titlecase_entity(s["text"])
        for g in grams:
            if not g:
                continue
            score[g] += w
            if pw is not None:
                prior[g] += pw
            srcs[g].add(s["source"])
            entity[g] += 1 if ent else 0
            examples.setdefault(g, s["text"])
    phrases = []
    for g, sc in score.items():
        if len(g.replace(" ", "")) < 3:
            continue
        names = [t for t in g.split() if t in proper]
        phrases.append({"phrase": g, "score": sc, "prior": prior.get(g), "sources": sorted(srcs[g]),
                        "entity_hits": entity[g], "names": names, "example": examples[g]})
    phrases.sort(key=lambda p: -p["score"])
    return phrases

# ---------------------------------------------------------------- velocity
def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return {}

def apply_velocity(phrases, history, today):
    """velocity > 1 = growing. Uses a prior from the snapshot if given, else average of past runs."""
    for p in phrases:
        past = [v for d, v in history.get(p["phrase"], {}).items() if d != today]
        base = p["prior"] if p["prior"] is not None else (sum(past) / len(past) if past else None)
        if base is None:
            p["velocity"], p["stage"] = 1.0, "new to tracker"
        else:
            p["velocity"] = (p["score"] + 0.1) / (base + 0.1)
            p["stage"] = ("breaking out" if p["velocity"] >= 3 else "rising" if p["velocity"] >= 1.3
                          else "steady" if p["velocity"] >= 0.8 else "fading")
    return phrases

def save_history(history, phrases, today, keep=300):
    for p in phrases[:keep]:
        history.setdefault(p["phrase"], {})[today] = round(p["score"], 3)
    HISTORY.write_text(json.dumps(history, indent=1))

# ---------------------------------------------------------------- risk
# Starter list. Extend it, and always run a real trademark search before buying.
KNOWN_MARKS = set("""apple google amazon meta facebook instagram tiktok youtube netflix disney marvel nike adidas
ikea tesla openai chatgpt claude anthropic microsoft spotify twitter x nfl nba mlb nhl fifa olympics
starbucks mcdonalds cocacola pepsi uber airbnb samsung sony nintendo playstation xbox lego barbie""".split())

def risk(p):
    words = set(p["phrase"].split())
    if words & KNOWN_MARKS:
        return "high", "contains a known brand"
    if p.get("names"):
        return "high", "contains a name: " + ", ".join(p["names"])
    if p["entity_hits"] and p["entity_hits"] >= max(1, len(p["sources"])):
        return "high", "looks like a person, title or brand"
    if p["entity_hits"]:
        return "medium", "sometimes appears as a proper noun"
    return "low", "generic phrase"

# ---------------------------------------------------------------- candidates
TLD_WEIGHT = {"com": 1.0, "ai": 0.8, "co": 0.65, "io": 0.6, "app": 0.55, "xyz": 0.3}
AFFIXES = [("", ""), ("get", ""), ("", "hq"), ("", "club"), ("the", "")]
FALLBACK_PRICE = {"com": (11.08, 11.08), "ai": (71.40, 71.40), "co": (25.00, 27.00),
                  "io": (32.00, 46.00), "app": (14.00, 16.00), "xyz": (2.00, 12.98)}  # rough USD

def slug(phrase):
    return re.sub(r"[^a-z0-9]", "", phrase.replace("'", ""))

def brandability(name):
    L = len(name)
    s = 1.0 if L <= 8 else 0.85 if L <= 11 else 0.65 if L <= 14 else 0.4
    if any(c.isdigit() for c in name):
        s *= 0.75
    vowels = sum(c in "aeiouy" for c in name) / max(L, 1)
    if vowels < 0.2:
        s *= 0.7
    if re.search(r"(.)\1\1", name):
        s *= 0.7
    return s

def candidates(phrases, top_phrases=40, tlds=TLD_WEIGHT, include_risky=False):
    out, used, hidden = [], 0, 0
    for p in phrases:
        if used >= top_phrases:
            break
        base = slug(p["phrase"])
        if not base or len(base) > 16:
            continue
        rlevel, rwhy = risk(p)
        if rlevel == "high" and not include_risky:
            hidden += 1
            continue
        used += 1
        trend = p["score"] * min(p["velocity"], 5) ** 0.5
        for pre, suf in AFFIXES:
            name = pre + base + suf
            if len(name) > 18:
                continue
            for tld, tw in tlds.items():
                exact = (pre, suf) == ("", "")
                out.append({"domain": f"{name}.{tld}", "phrase": p["phrase"], "stage": p["stage"],
                            "velocity": round(p["velocity"], 2), "risk": rlevel, "risk_why": rwhy,
                            "sources": ",".join(p["sources"]), "example": p["example"],
                            "score": trend * brandability(name) * tw * (1.0 if exact else 0.6)
                                     * {"low": 1, "medium": 0.6, "high": 0.15}[rlevel]})
    out.sort(key=lambda c: -c["score"])
    return out, hidden

# ---------------------------------------------------------------- availability + price
def tld_prices():
    """Porkbun's public price list (no key needed). Falls back to a rough built-in table."""
    errs = []
    for url in ("https://api.porkbun.com/api/json/v3/pricing/get",
                "https://porkbun.com/api/json/v3/pricing/get"):
        for data in (b"{}", None):
            try:
                _, body = _get(url, data=data)
                pr = json.loads(body)["pricing"]
                num = lambda x: float(str(x).replace(",", ""))
                return {t: (num(v["registration"]), num(v["renewal"])) for t, v in pr.items()}, "porkbun"
            except Exception as e:
                errs.append(f"{url.split('/')[2]} {'POST' if data else 'GET'}: {e}")
    print("  price list unavailable, using fallback table:\n    " + "\n    ".join(errs), file=sys.stderr)
    return FALLBACK_PRICE, "fallback table (approximate)"

_BOOT = None
def rdap_servers():
    """IANA's official map of TLD -> RDAP server. A TLD missing from it can't be checked by RDAP."""
    global _BOOT
    if _BOOT is None:
        _, body = _get("https://data.iana.org/rdap/dns.json")
        _BOOT = {tld: urls[0] for tlds, urls in json.loads(body)["services"] for tld in tlds}
    return _BOOT

def resolves(domain):
    try:
        socket.setdefaulttimeout(4)
        socket.getaddrinfo(domain, None)
        return True
    except OSError:
        return False

def rdap_status(domain):
    """
    taken        registry has a record, or the name resolves in DNS
    available?   registry says not found AND no DNS; still confirm at a registrar (premium/reserved)
    unknown      no RDAP server for this TLD, or the lookup failed: never treated as available
    """
    if resolves(domain):
        return "taken"
    tld = domain.rsplit(".", 1)[1]
    try:
        base = rdap_servers().get(tld)
    except Exception as e:
        return f"unknown (bootstrap {e.__class__.__name__})"
    if not base:
        return "unknown (no RDAP for ." + tld + ")"
    try:
        code, _ = _get(base.rstrip("/") + "/domain/" + domain, timeout=10,
                       headers={"Accept": "application/rdap+json"})
        return "taken" if code == 200 else f"unknown ({code})"
    except urllib.error.HTTPError as e:
        return "available?" if e.code == 404 else f"unknown ({e.code})"
    except Exception as e:
        return f"unknown ({e.__class__.__name__})"

def check(cands, limit, prices, delay=0.4):
    checked = 0
    for c in cands:
        tld = c["domain"].rsplit(".", 1)[1]
        reg, ren = prices.get(tld, (None, None))
        c["register_usd"], c["renew_usd"] = reg, ren
        if checked < limit:
            c["status"] = rdap_status(c["domain"]); checked += 1; time.sleep(delay)
        else:
            c["status"] = "not checked"
    return cands

# ---------------------------------------------------------------- output
COLS = ["rank", "domain", "status", "score", "phrase", "stage", "velocity", "risk", "risk_why",
        "register_usd", "renew_usd", "sources", "example"]

def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(rows, 1):
            w.writerow({**r, "rank": i, "score": round(r["score"], 2)})

def write_html(rows, phrases, path, meta):
    e = html.escape
    def money(v): return "" if v is None else f"${v:,.2f}"
    trs = "".join(
        f"<tr class='{e(r['risk'])}'><td>{i}</td><td><b>{e(r['domain'])}</b></td><td>{e(r['status'])}</td>"
        f"<td>{r['score']:.1f}</td><td>{e(r['phrase'])}</td><td>{e(r['stage'])} ({r['velocity']}×)</td>"
        f"<td>{e(r['risk'])}: {e(r['risk_why'])}</td><td>{money(r.get('register_usd'))}</td>"
        f"<td>{money(r.get('renew_usd'))}</td></tr>" for i, r in enumerate(rows, 1))
    ph = "".join(f"<li><b>{e(p['phrase'])}</b> · {e(p['stage'])} · {', '.join(map(e, p['sources']))}</li>"
                 for p in phrases[:25])
    Path(path).write_text(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>trendnamer report</title>
<style>body{{font:15px/1.5 system-ui;margin:2rem;color:#111}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:6px 8px;border-bottom:1px solid #ddd;text-align:left}}tr.high{{color:#999}}
tr.medium td:nth-child(7){{color:#b45309}}</style></head><body>
<h1>trendnamer · {e(meta['date'])}</h1><p>{e(meta['note'])}</p>
<table><tr><th>#</th><th>Domain</th><th>Status</th><th>Score</th><th>Phrase</th><th>Trend</th><th>Risk</th>
<th>Register</th><th>Renew</th></tr>{trs}</table><h2>Top phrases</h2><ol>{ph}</ol></body></html>""")

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="google_trends,wikipedia,hackernews,google_news,bluesky,reddit")
    ap.add_argument("--snapshot", help="read signals from a JSON file instead of live sources")
    ap.add_argument("--save-snapshot", help="write today's raw signals to this JSON file")
    ap.add_argument("--no-check", action="store_true", help="skip RDAP availability lookups")
    ap.add_argument("--check-limit", type=int, default=60, help="max domains to look up")
    ap.add_argument("--top", type=int, default=50, help="rows in the report")
    ap.add_argument("--include-risky", action="store_true", help="keep high-risk (brand/person) names")
    ap.add_argument("--out", default="trendnamer_report")
    a = ap.parse_args()

    today = dt.date.today().isoformat()
    if a.snapshot:
        snap = json.loads(Path(a.snapshot).read_text())
        signals, today = snap["signals"], snap.get("date", today)
        print(f"Loaded {len(signals)} signals from {a.snapshot}", file=sys.stderr)
    else:
        print("Collecting signals:", file=sys.stderr)
        signals = collect(a.sources.split(","))
    if a.save_snapshot:
        Path(a.save_snapshot).write_text(json.dumps({"date": today, "signals": signals}, indent=1))
    if not signals:
        sys.exit("No signals collected.")

    history = load_history()
    phrases = apply_velocity(extract_phrases(signals), history, today)
    save_history(history, phrases, today)

    cands, dropped = candidates(phrases, include_risky=a.include_risky)
    seen, rows = set(), []
    for c in cands:                       # at most 3 domains per phrase so one trend can't flood the list
        k = c["phrase"]
        if sum(r["phrase"] == k for r in rows) < 3 and c["domain"] not in seen:
            rows.append(c); seen.add(c["domain"])
        if len(rows) >= a.top:
            break

    prices, price_src = (FALLBACK_PRICE, "fallback table") if a.no_check else tld_prices()
    rows = check(rows, 0 if a.no_check else a.check_limit, prices)
    if not a.no_check:
        rows.sort(key=lambda r: (r["status"] != "available?", -r["score"]))
    note = (f"{len(signals)} signals → {len(phrases)} phrases → {len(rows)} domains"
            f"{'' if a.include_risky else f' ({dropped} brand/person phrases skipped; --include-risky shows them)'}"
            f". Prices: {price_src}. "
            "'available?' means no registration record; confirm at a registrar (premium/reserved names "
            "aren't visible to RDAP). Run a trademark search before buying anything.")
    write_csv(rows, a.out + ".csv"); write_html(rows, phrases, a.out + ".html", {"date": today, "note": note})
    print(f"\n{note}\nWrote {a.out}.csv and {a.out}.html\n", file=sys.stderr)
    for i, r in enumerate(rows[:15], 1):
        print(f"{i:2d}. {r['domain']:24s} {r['status']:12s} {r['stage']:14s} risk={r['risk']:6s} {r['phrase']}")

if __name__ == "__main__":
    main()
