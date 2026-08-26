#!/usr/bin/env python3
"""
Daily refresh of the OpenMinds blog GA4/GSC dashboard — headless cloud version.

Runs inside GitHub Actions in the openmindsmy/om-blog-dashboard repo.
Reads index.html from the checkout, rebuilds the data region between
/*__DATA_START__*/ and /*__DATA_END__*/, writes it back. The workflow
commits the result; GitHub Pages serves it.

Env vars (repo secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON  - service-account key JSON (GA4 + GSC read access)
  SEMRUSH_API_KEY              - Semrush Analytics API key
  ANTHROPIC_API_KEY            - optional; polishes analysis bullets + working titles

Ported from the Cowork scheduled task "om-blog-dashboard-daily" (Aug 2026).
Weekly topic picks are GAP-ONLY (user decision, 26 Aug 2026): Refresh rows
appear in the backlog but are never picked.
"""

import json, math, os, re, sys, time
import datetime as dt
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

# ---------------------------------------------------------------- constants
KL = ZoneInfo("Asia/Kuala_Lumpur")
GA4_PROPERTY = "277421164"
GSC_SITE = "https://www.openmindsresources.com/"
SEMRUSH_TARGET = "openmindsresources.com"
DAILY_START = "2026-01-01"  # window start for DAILY + aicite; bump each January
INDEX_HTML = os.environ.get("INDEX_HTML", "index.html")
SHEET_CSV = ("https://docs.google.com/spreadsheets/d/"
             "1dBjpJLlAuzgVqWhkgboVnNrHU1BZdYgRp5jlFJ480hs/gviz/tq?tqx=out:csv&gid=0")
AI_SOURCE_REGEX = (r"chatgpt\.com|chat\.openai\.com|openai\.com|claude\.ai|"
                   r"gemini\.google\.com|bard\.google\.com|copilot\.microsoft\.com|"
                   r"copilot\.com|perplexity\.ai")
SEM_SEEDS = ["martech", "digital marketing malaysia", "digital marketing",
             "google ads malaysia", "google ads", "ai marketing",
             "marketing automation", "seo malaysia", "tiktok ads", "content marketing"]
NOISE_RE = re.compile(r"login|register|account|manager|cancel|delete|remove|block|stop|"
                      r"download|apk|cancer|clinical|medical|hospital|institute|forex|"
                      r"stock market|jobs|salary|course|training|internship")
STOPWORDS = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "is",
             "are", "how", "what", "why", "when", "which", "best", "you", "your", "my",
             "me", "we", "us", "vs", "versus", "do", "does", "can", "it", "its", "at",
             "by", "from", "as", "be", "this", "that", "should", "i", "top", "guide"}
SYNONYMS = {"adwords": "ads", "advertising": "ads", "advert": "ads",
            "price": "cost", "pricing": "cost", "charge": "cost", "rate": "cost",
            "fee": "cost"}
QUESTION_RE = re.compile(r"^(who|what|when|where|why|how|is|are|can|does|do|should|will)\b")
EXCLUDE_PATH_RE = re.compile(r"^/blog/(category|tag|author|page)/")

MARK_START = "/*__DATA_START__*/"
MARK_END = "/*__DATA_END__*/"
CONST_ORDER = ["DAILY", "FIXED", "AIVIS", "WORKFLOW_BASE", "COMPARE", "TOPICS", "GENERATED"]

NOTES = []  # things skipped / degraded, surfaced in the run log


def log(msg):
    print(f"[refresh] {msg}", flush=True)


# ---------------------------------------------------------------- dates
def kl_today():
    return dt.datetime.now(KL).date()


def kl_yesterday():
    return kl_today() - dt.timedelta(days=1)


def d2s(d):
    return d.strftime("%Y-%m-%d")


def d2int(s):  # "2026-08-26" or "20260826" -> 20260826
    return int(s.replace("-", ""))


# ---------------------------------------------------------------- Google auth
def google_session():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("FATAL: GOOGLE_SERVICE_ACCOUNT_JSON not set — cannot refresh. Aborting (no commit).")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/analytics.readonly",
                "https://www.googleapis.com/auth/webmasters.readonly"])
    return AuthorizedSession(creds)


def ga4_report(sess, body, max_rows=250000):
    """runReport with offset paging; returns list of row dicts {dims:[...], mets:[...]}"""
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport"
    rows, offset = [], 0
    body = dict(body)
    body["limit"] = str(min(max_rows, 250000))
    while True:
        body["offset"] = str(offset)
        r = sess.post(url, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"GA4 runReport {r.status_code}: {r.text[:500]}")
        data = r.json()
        batch = data.get("rows", [])
        for row in batch:
            rows.append({"dims": [d.get("value", "") for d in row.get("dimensionValues", [])],
                         "mets": [m.get("value", "0") for m in row.get("metricValues", [])]})
        total = int(data.get("rowCount", 0))
        offset += len(batch)
        if offset >= total or not batch or offset >= max_rows:
            return rows


def blog_filter(field="pagePath"):
    return {"filter": {"fieldName": field,
                       "stringFilter": {"matchType": "BEGINS_WITH", "value": "/blog"}}}


def gsc_query(sess, start, end, dimensions, row_limit=25000):
    url = ("https://www.googleapis.com/webmasters/v3/sites/"
           f"{quote(GSC_SITE, safe='')}/searchAnalytics/query")
    body = {"startDate": start, "endDate": end, "dimensions": dimensions,
            "rowLimit": row_limit, "startRow": 0,
            "dimensionFilterGroups": [{"filters": [
                {"dimension": "page", "operator": "contains", "expression": "/blog/"}]}]}
    rows = []
    while True:
        r = sess.post(url, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"GSC query {r.status_code}: {r.text[:500]}")
        batch = r.json().get("rows", [])
        rows.extend(batch)
        if len(batch) < row_limit:
            return rows
        body["startRow"] += row_limit


# ---------------------------------------------------------------- step 1: DAILY
def build_daily(sess, yday):
    dr = [{"startDate": DAILY_START, "endDate": d2s(yday)}]
    log("DAILY: per-page …")
    r = ga4_report(sess, {"dateRanges": dr,
                          "dimensions": [{"name": "date"}, {"name": "pagePath"}],
                          "metrics": [{"name": n} for n in
                                      ["screenPageViews", "sessions", "engagedSessions",
                                       "userEngagementDuration", "activeUsers"]],
                          "dimensionFilter": blog_filter()})
    pages, pidx, rows = [], {}, []
    for row in r:
        p = row["dims"][1]
        if p not in pidx:
            pidx[p] = len(pages); pages.append(p)
        rows.append([int(row["dims"][0]), pidx[p],
                     int(float(row["mets"][0])), int(float(row["mets"][1])),
                     int(float(row["mets"][2])), round(float(row["mets"][3])),
                     int(float(row["mets"][4]))])

    def simple(dim):
        rr = ga4_report(sess, {"dateRanges": dr,
                               "dimensions": [{"name": "date"}, {"name": dim}],
                               "metrics": [{"name": "sessions"}],
                               "dimensionFilter": blog_filter()})
        return [[int(x["dims"][0]), x["dims"][1], int(float(x["mets"][0]))] for x in rr]

    log("DAILY: channel/sm/nvr …")
    chan = simple("sessionDefaultChannelGroup")
    sm = simple("sessionSourceMedium")
    nvr = simple("newVsReturning")
    log("DAILY: entrances …")
    ent_r = ga4_report(sess, {"dateRanges": dr, "dimensions": [{"name": "date"}],
                              "metrics": [{"name": "sessions"}],
                              "dimensionFilter": blog_filter("landingPagePlusQueryString")})
    ent = [[int(x["dims"][0]), int(float(x["mets"][0]))] for x in ent_r]
    return {"pages": pages, "rows": rows, "chan": chan, "sm": sm, "nvr": nvr, "ent": ent}


# ---------------------------------------------------------------- step 2: FIXED
def strip_query(path):
    return path.split("?")[0]


def is_article(path):
    return path.startswith("/blog/") and path != "/blog/" and not EXCLUDE_PATH_RE.match(path)


def build_fixed(sess, yday):
    cur_s, cur_e = yday - dt.timedelta(days=27), yday
    prv_s, prv_e = yday - dt.timedelta(days=55), yday - dt.timedelta(days=28)

    def views28(a, b):
        rr = ga4_report(sess, {"dateRanges": [{"startDate": d2s(a), "endDate": d2s(b)}],
                               "dimensions": [{"name": "pagePath"}],
                               "metrics": [{"name": "screenPageViews"}],
                               "dimensionFilter": blog_filter(), "limit": "300"}, max_rows=300)
        return {x["dims"][0]: int(float(x["mets"][0])) for x in rr}

    log("FIXED: movers …")
    cur, prv = views28(cur_s, cur_e), views28(prv_s, prv_e)
    allp = set(cur) | set(prv)
    deltas = sorted(((p, prv.get(p, 0), cur.get(p, 0)) for p in allp),
                    key=lambda t: t[2] - t[1])
    risers = [[p, a, b] for p, a, b in reversed(deltas) if b - a > 0][:6]
    fallers = [[p, a, b] for p, a, b in deltas if b - a < 0][:6]

    log("FIXED: GSC queries …")
    qrows = gsc_query(sess, d2s(cur_s), d2s(cur_e), ["query"])
    qrows.sort(key=lambda r: r.get("impressions", 0), reverse=True)
    search = [[r["keys"][0], int(r.get("clicks", 0)), int(r.get("impressions", 0)),
               round(r.get("ctr", 0.0), 4), round(r.get("position", 0.0), 1)]
              for r in qrows[:21]]
    full_queries = [[r["keys"][0], int(r.get("clicks", 0)), int(r.get("impressions", 0)),
                     r.get("ctr", 0.0), r.get("position", 0.0)] for r in qrows]

    log("FIXED: GSC positions …")
    prow = gsc_query(sess, d2s(cur_s), d2s(cur_e), ["page"])
    agg = {}
    for r in prow:
        path = strip_query(r["keys"][0].replace("https://www.openmindsresources.com", ""))
        if not is_article(path):
            continue
        imp, clk, pos = int(r.get("impressions", 0)), int(r.get("clicks", 0)), r.get("position", 0.0)
        a = agg.setdefault(path, [0, 0, 0.0])
        a[0] += imp; a[1] += clk; a[2] += pos * imp
    positions = sorted(([p, round(v[2] / v[0], 1), v[0], v[1]]
                        for p, v in agg.items() if v[0] >= 5), key=lambda x: x[1])

    log("FIXED: journey …")
    jr = ga4_report(sess, {"dateRanges": [{"startDate": d2s(cur_s), "endDate": d2s(cur_e)}],
                           "dimensions": [{"name": "pageReferrer"}, {"name": "pagePath"}],
                           "metrics": [{"name": "screenPageViews"}],
                           "dimensionFilter": {"andGroup": {"expressions": [
                               blog_filter(),
                               {"filter": {"fieldName": "pageReferrer",
                                           "stringFilter": {"matchType": "CONTAINS",
                                                            "value": "openmindsresources.com/blog"}}}]}},
                           "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
                           "limit": "80"}, max_rows=80)
    groups = {}
    for row in jr:
        frm = strip_query(re.sub(r"^https?://[^/]+", "", row["dims"][0])) or "/"
        to = row["dims"][1]
        if frm == to:
            continue
        g = groups.setdefault(frm, {})
        g[to] = g.get(to, 0) + int(float(row["mets"][0]))
    journey = [{"from": f, "total": sum(d.values()),
                "dests": sorted(d.items(), key=lambda kv: -kv[1])}
               for f, d in groups.items()]
    journey = sorted(journey, key=lambda j: -j["total"])[:4]
    for j in journey:
        j["dests"] = [[p, n] for p, n in j["dests"]]

    fixed = {"risers": risers, "fallers": fallers, "search": search,
             "positions": positions, "journey": journey,
             "analysis": {"did_well": [], "improve": [], "actions": []}}
    return fixed, full_queries


# ---------------------------------------------------------------- step 2b: workflow tags
def build_workflow_base(existing):
    try:
        txt = requests.get(SHEET_CSV, timeout=60).text
    except Exception as e:
        NOTES.append(f"sheet fetch failed ({e}); WORKFLOW_BASE unchanged")
        return sorted(existing), 0
    import csv, io
    body = txt.split("\n\n", 1)[-1] if "\n\n" in txt[:2000] else txt
    found = set()
    for row in csv.reader(io.StringIO(body)):
        if len(row) > 13 and row[10].strip().lower() == "published":
            m = re.search(r"/blog/[^\s\"'?#]+", row[13])
            if m:
                p = m.group(0)
                found.add(p if p.endswith("/") else p + "/")
    union = sorted(set(existing) | found)
    log(f"WORKFLOW_BASE: sheet published={len(found)}, existing={len(existing)}, union={len(union)}")
    return union, len(found)


# ---------------------------------------------------------------- step 2c: semrush + aicite
def semrush_get(params):
    key = os.environ.get("SEMRUSH_API_KEY")
    if not key:
        raise RuntimeError("SEMRUSH_API_KEY not set")
    p = dict(params); p["key"] = key
    r = requests.get("https://api.semrush.com/", params=p, timeout=60)
    if r.status_code != 200 or r.text.startswith("ERROR"):
        raise RuntimeError(f"Semrush {r.status_code}: {r.text[:200]}")
    lines = [l for l in r.text.strip().split("\n") if l]
    hdr = lines[0].split(";")
    return [dict(zip(hdr, l.split(";"))) for l in lines[1:]]


def semrush_domain_block():
    """Returns the AIVIS 'semrush' block. SERP feature codes: 1=Knowledge panel,
    11=Featured snippet, 21=People also ask, 52=AI overview (FKn = triggered-by counts)."""
    cols = "Rk,Or,Ot,FK1,FK11,FK21,FK52"
    def one(db):
        rows = semrush_get({"type": "domain_ranks", "domain": SEMRUSH_TARGET,
                            "database": db, "export_columns": cols})
        return rows[0] if rows else {}
    my, us = one("my"), one("us")
    # positions 1-3 (MY): count top-3 organic keywords
    pos13 = 0
    try:
        org = semrush_get({"type": "domain_organic", "domain": SEMRUSH_TARGET,
                           "database": "my", "export_columns": "Ph,Po",
                           "display_limit": 1000, "display_sort": "po_asc"})
        pos13 = sum(1 for r in org if r.get("Position", r.get("Po", "99")).isdigit()
                    and int(r.get("Position", r.get("Po"))) <= 3)
    except Exception as e:
        NOTES.append(f"semrush domain_organic failed ({e}); pos13_my=0")

    def gi(d, *keys):
        for k in keys:
            if k in d and str(d[k]).strip().isdigit():
                return int(d[k])
        return 0
    return {"rank_my": gi(my, "Rank", "Rk"), "kw_my": gi(my, "Organic Keywords", "Or"),
            "pos13_my": pos13, "traffic_my": gi(my, "Organic Traffic", "Ot"),
            "ai_overview_my": gi(my, "AI overview", "FK52"),
            "paa_my": gi(my, "People also ask", "FK21"),
            "kp_my": gi(my, "Knowledge panel", "FK1"),
            "featured_my": gi(my, "Featured Snippet", "FK11"),
            "kw_us": gi(us, "Organic Keywords", "Or"),
            "ai_overview_us": gi(us, "AI overview", "FK52"),
            "paa_us": gi(us, "People also ask", "FK21"),
            "traffic_us": gi(us, "Organic Traffic", "Ot")}


AI_LABELS = [("chatgpt", "ChatGPT"), ("openai", "ChatGPT"), ("gemini", "Gemini"),
             ("bard", "Gemini"), ("claude", "Claude"), ("copilot", "Copilot"),
             ("perplexity", "Perplexity")]


def ai_label(src):
    s = src.lower()
    for k, v in AI_LABELS:
        if k in s:
            return v
    return src


def build_aicite(sess, yday):
    rr = ga4_report(sess, {
        "dateRanges": [{"startDate": DAILY_START, "endDate": d2s(yday)}],
        "dimensions": [{"name": "date"}, {"name": "sessionSource"},
                       {"name": "landingPagePlusQueryString"}],
        "metrics": [{"name": "sessions"}, {"name": "engagedSessions"},
                    {"name": "userEngagementDuration"}],
        "dimensionFilter": {"andGroup": {"expressions": [
            {"filter": {"fieldName": "sessionSource",
                        "stringFilter": {"matchType": "FULL_REGEXP", "value": AI_SOURCE_REGEX}}},
            blog_filter("landingPagePlusQueryString")]}}}, max_rows=5000)
    merged = {}
    for row in rr:
        key = (int(row["dims"][0]), ai_label(row["dims"][1]), strip_query(row["dims"][2]))
        a = merged.setdefault(key, [0, 0, 0.0])
        a[0] += int(float(row["mets"][0])); a[1] += int(float(row["mets"][1]))
        a[2] += float(row["mets"][2])
    daily = [[d, l, p, s, e, round(dur)] for (d, l, p), (s, e, dur) in sorted(merged.items())]
    plat, pages = {}, {}
    for d, l, p, s, e, dur in daily:
        plat[l] = plat.get(l, 0) + s
        a = pages.setdefault(p, [0, 0, 0.0])
        a[0] += s; a[1] += e; a[2] += dur
    total = sum(plat.values())
    return {"window": f"1 Jan – {yday.strftime('%-d %b %Y')}", "total": total,
            "platforms": sorted(([l, s] for l, s in plat.items()), key=lambda x: -x[1]),
            "pages": sorted(([p, v[0], round(100 * v[1] / v[0]) if v[0] else 0,
                              round(v[2] / v[0]) if v[0] else 0]
                             for p, v in pages.items()), key=lambda x: -x[1]),
            "daily": daily}


# ---------------------------------------------------------------- step 2d: TOPICS
def norm_token(t):
    t = SYNONYMS.get(t, t)
    t = t.replace("ization", "isation")
    if t.endswith("ize"):
        t = t[:-3] + "ise"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return SYNONYMS.get(t, t)


def tokens(text):
    return {norm_token(w) for w in re.split(r"[^a-z0-9]+", text.lower())
            if len(w) > 2 and w not in STOPWORDS}


def is_question(q):
    return 1 if (QUESTION_RE.match(q.lower()) or "?" in q) else 0


def slug_tokens(path):
    return tokens(path.rstrip("/").rsplit("/", 1)[-1].replace("-", " "))


def coverage_match(qtok, articles):
    """articles: [(path, tokset, pos_or_None)] -> (covered_path, pos) or (None, None)"""
    best = (None, None)
    for path, atok, pos in articles:
        if not qtok:
            continue
        inter = len(qtok & atok)
        cov = inter / len(qtok)
        jac = inter / len(qtok | atok) if (qtok | atok) else 0
        if cov >= 0.6 and jac >= 0.5:
            if best[0] is None or (pos or 99) < (best[1] or 99):
                best = (path, pos)
    return best


def semrush_expand():
    out = []
    for seed in SEM_SEEDS:
        for typ in ("phrase_related", "phrase_questions"):
            cols_try = ["Ph,Nq,Cp,Kd,In", "Ph,Nq,Cp,Kd", "Ph,Nq,Cp"]
            rows = None
            for cols in cols_try:
                try:
                    rows = semrush_get({"type": typ, "phrase": seed, "database": "my",
                                        "export_columns": cols, "display_limit": 25,
                                        "display_sort": "nq_desc"})
                    break
                except RuntimeError as e:
                    if "NOTHING FOUND" in str(e).upper():
                        rows = []
                        break
                    continue
            if rows is None:
                NOTES.append(f"semrush {typ}('{seed}') failed")
                continue
            for r in rows:
                ph = r.get("Keyword", r.get("Ph", "")).strip()
                if not ph:
                    continue
                vol = int(r.get("Search Volume", r.get("Nq", "0")) or 0)
                kd = r.get("Keyword Difficulty Index", r.get("Kd", ""))
                kd = float(kd) if str(kd).replace(".", "", 1).isdigit() else None
                intent = r.get("Intents", r.get("In", ""))
                qform = 1 if typ == "phrase_questions" else is_question(ph)
                informational = ("1" in str(intent).split(",")) if intent else False
                if not (informational or qform):
                    continue
                out.append({"t": ph, "d": vol, "kd": kd, "q": qform})
            time.sleep(0.3)
    # dedupe by phrase, keep max volume
    ded = {}
    for c in out:
        k = c["t"].lower()
        if k not in ded or c["d"] > ded[k]["d"]:
            ded[k] = c
    return list(ded.values())


def build_topics(full_queries, compare_articles, positions, yday, old_topics, sem_ok, sem_rows):
    # article token sets with GSC position
    posmap = {p: pos for p, pos, *_ in [(r[0], r[1], r[2], r[3]) for r in positions]}
    articles = [(row[0], slug_tokens(row[0]), posmap.get(row[0]))
                for row in compare_articles]

    cands = []
    for q, clicks, impr, ctr, pos in full_queries:
        if impr < 8 or pos <= 10.5 or NOISE_RE.search(q.lower()):
            continue
        cands.append({"t": q, "s": "GSC", "d": impr, "p": round(pos, 1), "kd": None,
                      "q": is_question(q)})
    if sem_ok:
        for c in sem_rows:
            if NOISE_RE.search(c["t"].lower()):
                continue
            cands.append({"t": c["t"], "s": "SEM", "d": c["d"], "p": None,
                          "kd": c["kd"], "q": c["q"]})

    # coverage
    for c in cands:
        path, apos = coverage_match(tokens(c["t"]), articles)
        if path and (apos is not None and apos <= 10):
            c["drop"] = True
        elif path:
            c["k"], c["a"], c["ap"] = "Refresh", path, apos
        else:
            c["k"], c["a"], c["ap"] = "Gap", None, None
    cands = [c for c in cands if not c.get("drop")]

    # cluster (jaccard >= 0.7)
    cands.sort(key=lambda c: -c["d"])
    clusters = []
    for c in cands:
        ct = tokens(c["t"])
        placed = False
        for cl in clusters:
            u, i = ct | cl["tok"], ct & cl["tok"]
            if u and len(i) / len(u) >= 0.7 and cl["s"] == c["s"]:
                cl["d"] += c["d"]; cl["v"] += 1
                placed = True
                break
        if not placed:
            clusters.append({**c, "tok": ct, "v": 1})

    # score
    maxd = {"GSC": max([c["d"] for c in clusters if c["s"] == "GSC"] + [1]),
            "SEM": max([c["d"] for c in clusters if c["s"] == "SEM"] + [1])}
    items = []
    for c in clusters:
        base = math.log1p(c["d"]) / math.log1p(maxd[c["s"]])
        if c["s"] == "SEM":
            base *= 0.85
        mult = 1.0 if c["k"] == "Refresh" else (0.9 if c["s"] == "GSC" else 0.72)
        if c["s"] == "SEM":
            mult *= max(0.35, 1 - (c["kd"] if c["kd"] is not None else 50) / 150)
        sc = base * mult * (1.12 if c["q"] else 1.0)
        why = (f"Ranks #{c['ap']} via {c['a']} — refresh to push onto page 1"
               if c["k"] == "Refresh" else
               (f"{c['d']} impressions/28d with no covering article" if c["s"] == "GSC"
                else f"MY search volume {c['d']}, no covering article"))
        items.append({"t": c["t"], "k": c["k"], "s": c["s"], "d": c["d"],
                      "p": c.get("p"), "kd": c.get("kd"),
                      "sc": max(1, min(100, round(100 * sc))), "q": c["q"],
                      "a": c["a"], "n": why, "ti": "", "also": [], "v": c["v"]})
    items.sort(key=lambda x: -x["sc"])
    if not sem_ok and old_topics:
        olds = [i for i in old_topics.get("items", []) if i.get("s") == "SEM"]
        have = {i["t"].lower() for i in items}
        items += [i for i in olds if i["t"].lower() not in have]
        NOTES.append(f"Semrush unavailable — carried {len(olds)} old SEM rows")

    iso = kl_yesterday().isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    bymap = {i["t"]: i for i in items}
    old_items = {i["t"]: i for i in (old_topics or {}).get("items", [])}
    picks = []

    def add_also(item):
        if item["a"]:
            item["also"] = [i["t"] for i in items
                            if i["a"] == item["a"] and i["t"] != item["t"]][:4]

    gap_pool = [i for i in items if i["k"] == "Gap"]

    if old_topics and old_topics.get("week") == week:
        # carry over, but GAP-ONLY: swap out any Refresh picks
        for t in old_topics.get("picks", []):
            old = old_items.get(t, {})
            if old.get("k") == "Refresh":
                continue  # replaced below
            if t not in bymap:  # keep resolvable: re-inject the old row
                row = dict(old) if old else {"t": t, "k": "Gap", "s": "GSC", "d": 0,
                                             "p": None, "kd": None, "sc": 1, "q": 0,
                                             "a": None, "n": "carried over", "ti": t.title(),
                                             "also": [], "v": 1}
                items.append(row); bymap[t] = row
            bymap[t]["ti"] = old.get("ti") or bymap[t]["ti"] or t.title()
            picks.append(t)
        for g in gap_pool:  # top-ups for dropped Refresh picks
            if len(picks) >= 6:
                break
            if g["t"] not in picks:
                picks.append(g["t"]); add_also(g)
        carried = True
    else:
        used_articles = set()
        for g in gap_pool:
            if len(picks) >= 6:
                break
            akey = g["a"] or g["t"]
            if akey in used_articles:
                continue
            used_articles.add(akey)
            picks.append(g["t"]); add_also(g)
        carried = False

    log(f"TOPICS: week={week} carried={carried} picks={len(picks)} items={len(items)}")
    return {"week": week, "generated": kl_today().strftime("%-d %b %Y"),
            "picks": picks, "items": items}, carried


# ---------------------------------------------------------------- LLM polish (optional)
def anthropic_json(prompt, max_tokens=1500):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": "claude-haiku-4-5", "max_tokens": max_tokens,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=90)
        if r.status_code != 200:
            NOTES.append(f"anthropic {r.status_code}")
            return None
        txt = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        NOTES.append(f"anthropic failed ({e})")
        return None


def fill_analysis(fixed, daily, yday):
    cur_total = sum(r[2] for r in daily["rows"] if r[0] >= d2int(d2s(yday - dt.timedelta(days=27))))
    prv_total = sum(r[2] for r in daily["rows"]
                    if d2int(d2s(yday - dt.timedelta(days=55))) <= r[0] <= d2int(d2s(yday - dt.timedelta(days=28))))
    pct = round(100 * (cur_total - prv_total) / prv_total, 1) if prv_total else 0
    stats = {"views_28d": cur_total, "vs_prev_pct": pct,
             "risers": fixed["risers"][:3], "fallers": fixed["fallers"][:3],
             "top_queries": fixed["search"][:8],
             "page1_articles": [p for p in fixed["positions"] if p[1] <= 10][:8]}
    out = anthropic_json(
        "You write concise UK-English analysis bullets for a blog performance dashboard. "
        "Given this 28-day data, return ONLY JSON {\"did_well\":[3 strings],\"improve\":[3 strings],"
        "\"actions\":[3 strings]}. Each bullet one sentence, cite concrete numbers from the data.\n"
        + json.dumps(stats))
    if out and all(isinstance(out.get(k), list) and len(out[k]) >= 3
                   for k in ("did_well", "improve", "actions")):
        fixed["analysis"] = {k: out[k][:3] for k in ("did_well", "improve", "actions")}
        return
    # deterministic fallback
    r0 = fixed["risers"][0] if fixed["risers"] else ["–", 0, 0]
    f0 = fixed["fallers"][0] if fixed["fallers"] else ["–", 0, 0]
    q0 = fixed["search"][0] if fixed["search"] else ["–", 0, 0, 0, 0]
    fixed["analysis"] = {
        "did_well": [f"Blog page views reached {cur_total:,} in the last 28 days ({pct:+}% vs the previous 28).",
                     f"{r0[0]} rose from {r0[1]} to {r0[2]} views.",
                     f"'{q0[0]}' led search with {q0[2]:,} impressions and {q0[1]} clicks."],
        "improve": [f"{f0[0]} fell from {f0[1]} to {f0[2]} views — review freshness and internal links.",
                    "Several high-impression queries sit outside the top 10 — see the Topics tab.",
                    "CTR on top queries has room to grow — test sharper titles and meta descriptions."],
        "actions": ["Write this week's topic picks (Topics tab — all new gap topics).",
                    f"Refresh {f0[0]} and add internal links from the top journey pages.",
                    "Push near-page-1 articles over the line with targeted updates."]}
    NOTES.append("analysis bullets: deterministic fallback (no/failed ANTHROPIC_API_KEY)")


def fill_titles(topics):
    todo = [t for t in topics["picks"]]
    bym = {i["t"]: i for i in topics["items"]}
    missing = [t for t in todo if not bym.get(t, {}).get("ti")]
    if not missing:
        return
    out = anthropic_json(
        "Write a short, compelling UK-English working title for each blog topic query below "
        "(a Malaysian martech/digital-marketing blog). Return ONLY JSON mapping query -> title.\n"
        + json.dumps(missing))
    for t in missing:
        title = (out or {}).get(t) if isinstance(out, dict) else None
        bym[t]["ti"] = title or t.strip().capitalize()
    if not out:
        NOTES.append("working titles: fallback capitalisation used")


# ---------------------------------------------------------------- file rebuild
def parse_const(src, name):
    m = re.search(rf"const {name}\s*=", src)
    if not m:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(src[m.end():].lstrip())
        return val
    except Exception:
        return None


def rebuild(html, data):
    i, j = html.find(MARK_START), html.find(MARK_END)
    if i < 0 or j < 0 or html.count(MARK_START) != 1 or html.count(MARK_END) != 1:
        sys.exit("FATAL: data markers missing/duplicated in index.html — aborting, no commit.")
    head, tail = html[:i + len(MARK_START)], html[j:]
    cj = lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    region = "\n" + "\n".join(
        f"const {k}={cj(data[k]) if k != 'GENERATED' else json.dumps(data[k])};"
        for k in CONST_ORDER) + "\n"
    out = head + region + tail
    # validation
    assert out.rstrip().endswith("</html>"), "file does not end with </html>"
    for k in CONST_ORDER:
        assert f"const {k}=" in out, f"missing const {k}"
    t = data["TOPICS"]
    keys = {x["t"] for x in t["items"]}
    for p in t["picks"]:
        assert p in keys, f"pick '{p}' not in items"
        row = next(x for x in t["items"] if x["t"] == p)
        assert row["ti"], f"pick '{p}' has empty ti"
        assert row["k"] == "Gap", f"pick '{p}' is {row['k']} — picks must be Gap-only"
    return out


# ---------------------------------------------------------------- main
def main():
    yday = kl_yesterday()
    log(f"KL yesterday = {yday}")
    html = open(INDEX_HTML, encoding="utf-8").read()
    old_aivis = parse_const(html, "AIVIS") or {}
    old_workflow = parse_const(html, "WORKFLOW_BASE") or []
    old_topics = parse_const(html, "TOPICS")

    sess = google_session()
    daily = build_daily(sess, yday)
    fixed, full_queries = build_fixed(sess, yday)
    workflow, _sheet_n = build_workflow_base(old_workflow)

    # COMPARE
    cutoff = d2int(d2s(yday - dt.timedelta(days=27)))
    per_page = {}
    for r in daily["rows"]:
        p = strip_query(daily["pages"][r[1]])
        if not is_article(p):
            continue
        a = per_page.setdefault(p, [0, 0])
        a[1] += r[2]
        if r[0] >= cutoff:
            a[0] += r[2]
    posmap = {row[0]: row for row in fixed["positions"]}
    compare_articles = [[p, "W" if p in workflow else "M", v[0], v[1],
                         posmap.get(p, [0, None])[1],
                         posmap.get(p, [0, 0, 0, 0])[2] if p in posmap else 0,
                         posmap.get(p, [0, 0, 0, 0])[3] if p in posmap else 0]
                        for p, v in sorted(per_page.items(), key=lambda kv: -kv[1][1])]
    compare = {"publishedWorkflow": len(workflow), "articles": compare_articles}

    # Semrush
    sem_ok, sem_rows = True, []
    aivis = dict(old_aivis)
    try:
        aivis["semrush"] = semrush_domain_block()
        sem_rows = semrush_expand()
    except Exception as e:
        sem_ok = False
        NOTES.append(f"Semrush unavailable ({e}) — kept old semrush block & SEM topic rows")
    aivis["aicite"] = build_aicite(sess, yday)

    topics, carried = build_topics(full_queries, compare_articles, fixed["positions"],
                                   yday, old_topics, sem_ok, sem_rows)
    fill_titles(topics)
    fill_analysis(fixed, daily, yday)

    data = {"DAILY": daily, "FIXED": fixed, "AIVIS": aivis, "WORKFLOW_BASE": workflow,
            "COMPARE": compare, "TOPICS": topics,
            "GENERATED": kl_today().strftime("%-d %b %Y")}
    out = rebuild(html, data)
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(out)

    picks_summary = ", ".join(topics["picks"][:2])
    log(f"DONE. rows={len(daily['rows'])} workflow={len(workflow)} "
        f"week={topics['week']} ({'carried' if carried else 'new'}) top picks: {picks_summary}")
    for n in NOTES:
        log(f"NOTE: {n}")


if __name__ == "__main__":
    main()
