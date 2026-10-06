"""Scrape stillhiring.today's company list, pull each company's official ATS job board,
keep US software-engineering roles, and merge into docs/jobs.json (deduped, with first_seen).

Stdlib only. Run: python3 scrape.py
"""
import json, re, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DOCS = Path(__file__).parent / "docs"
JOBS_FILE, COMPANIES_FILE = DOCS / "jobs.json", DOCS / "companies.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
SHARE = "https://airtable.com/appPGrJqA2zH65k5I/shrI8dno1rMGKZM8y"
CLOSED_TTL_DAYS = 30  # drop closed jobs from the file after this long

# ---- filters (tweak here) ---------------------------------------------------
ENG = re.compile(r"\b(engineer|developer|programmer|swe|sde|sdet|member of technical staff|mts)\b", re.I)
SOFT = re.compile(  # title must also show it's software, or be a bare leveled "Senior Engineer"
    r"\b(software|swe|sde|sdet|developer|programmer|back[- ]?end|front[- ]?end|full[- ]?stack|devops|sre|"
    r"site reliability|platform|infrastructure|infra|ios|android|mobile|web|machine learning|ml|ai|llm|data|"
    r"cloud|security|qa|test automation|automation|api|distributed|application|applications|product engineer|"
    r"founding|technical staff|forward deployed|python|java|golang|go|rust|react|node|typescript|ruby|rails|"
    r"c\+\+|kotlin|swift|scala|\.net|php|ui|ux engineer|search|compiler|kernel|database|devex|"
    r"developer experience|reliability|observability|frameworks?|tools|tooling)\b"
    r"|^\W*((senior|sr\.?|staff|principal|lead|junior|jr\.?|founding)\s+)*engineer\b(\s+(i{1,3}|iv|[1-5]))?\W*$"
    r"|^\W*((senior|sr\.?|staff|principal|lead)\s+)+engineer\b", re.I)
NOT_SWE = re.compile(
    r"\b(sales|solutions?|support|customer|field|pre-?sales|implementation|professional services|"
    r"technical account|account|value|partner|manager|management|director|head|vp|vice president|chief|cto|"
    r"recruit\w*|talent|mechanical|electrical|electronics|power|civil|manufacturing|process|hardware|rf|"
    r"analog|chemical|structural|facilities|construction|marketing|helpdesk|help desk|technician|thermal|"
    r"propulsion|avionics|harness|mission|optical|materials|reliability engineer,? hardware|representative|"
    r"advocate|evangelist|relations|engagement|business development|biz ?dev)\b", re.I)
STATES = ("Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia Hawaii Idaho "
          "Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota "
          "Mississippi Missouri Montana Nebraska Nevada New_Hampshire New_Jersey New_Mexico New_York "
          "North_Carolina North_Dakota Ohio Oklahoma Oregon Pennsylvania Rhode_Island South_Carolina "
          "South_Dakota Tennessee Texas Utah Vermont Virginia Washington West_Virginia Wisconsin Wyoming "
          "District_of_Columbia").replace("_", " ").split()
STATES = [s.replace(" ", r"\s") for s in STATES]
ABBR = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
        "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
CITIES = ("San Francisco|SF|Bay Area|New York|NYC|Seattle|Austin|Boston|Chicago|Denver|Los Angeles|Atlanta|"
          "Palo Alto|Mountain View|San Jose|Sunnyvale|Menlo Park|Oakland|San Diego|Portland|Miami|Dallas|"
          "Houston|Philadelphia|Pittsburgh|Salt Lake|Boulder|Raleigh|Durham|Nashville|Minneapolis|Detroit|"
          r"Phoenix|Columbus|Washington D\.?C|Brooklyn|Cambridge, MA|Redwood City|Santa Monica|Irvine")
US_STRONG = re.compile(rf"\b(United States|USA|U\.S\.A?\.?|{'|'.join(STATES)}|{CITIES})\b", re.I)
US_CS = re.compile(r"\bUS\b|\bUS[-_ ]|,\s*(%s)\b" % "|".join(ABBR))  # case-sensitive: avoid "us"/"in"/"or"
NON_US = re.compile(r"\b(Canada|Toronto|Vancouver|Montreal|UK|United Kingdom|London|Ireland|Dublin|Germany|Berlin|"
                    r"Munich|France|Paris|Spain|Madrid|Barcelona|Portugal|Lisbon|Netherlands|Amsterdam|Poland|"
                    r"India|Bangalore|Bengaluru|Hyderabad|Pune|Israel|Tel Aviv|Australia|Sydney|Melbourne|"
                    r"Singapore|Japan|Tokyo|Brazil|Mexico|Argentina|Colombia|Costa Rica|EMEA|APAC|LATAM|Europe|"
                    r"Romania|Serbia|Ukraine|Czech|Prague|Hungary|Budapest|Denmark|Sweden|Norway|Finland|"
                    r"Estonia|Lithuania|Italy|Switzerland|Zurich|Belgium|Austria|Philippines|Vietnam|China|"
                    r"Korea|Taiwan|New Zealand|South Africa|Egypt|Turkey|Greece|Chile|Peru|Uruguay|UAE|Abu Dhabi|Dubai|Saudi|Riyadh|Qatar|Nigeria|Kenya|Pakistan|Indonesia|Malaysia|Thailand)\b", re.I)


def is_swe(title):
    return bool(ENG.search(title) and SOFT.search(title)) and not NOT_SWE.search(title)


def us_status(loc):
    """'yes' | 'maybe' | 'no'. ponytail: regex heuristic; misses odd city names, add to CITIES."""
    loc = loc or ""
    if US_STRONG.search(loc) or re.search(r"\bUS\b", loc):
        return "yes"
    if NON_US.search(loc):
        return "no"
    if US_CS.search(loc):  # ", CA" etc. only after ruling out "Berlin, DE" / "Pune, IN"
        return "yes"
    return "maybe"  # blank, "Remote", "2 Locations", unknown city


# ---- http -------------------------------------------------------------------
def get(url, data=None, headers=None, raw=False):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, h), timeout=25) as r:
                txt = r.read().decode("utf-8", "replace")
                return (txt, r.geturl()) if raw else json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code in (404, 400, 401, 403, 410) or attempt == 2:
                raise
        except Exception:
            if attempt == 2:
                raise
        time.sleep(2 * (attempt + 1))


# ---- company list -----------------------------------------------------------
def load_companies():
    html, _ = get(SHARE, raw=True)
    path = re.search(r'urlWithParams: "([^"]+)"', html).group(1).encode().decode("unicode_escape")
    t = get("https://airtable.com" + path, headers={
        "x-airtable-application-id": "appPGrJqA2zH65k5I", "x-requested-with": "XMLHttpRequest",
        "x-time-zone": "UTC", "x-user-locale": "en"})["data"]["table"]
    col = {c["name"]: c["id"] for c in t["columns"]}
    epd = next(c for c in t["columns"] if c["name"] == "Engineering, Product, Design Open Roles Found")
    swe_sel = {k for k, v in epd["typeOptions"]["choices"].items() if v["name"] == "Hiring Software Engineering"}
    out = []
    for r in t["rows"]:
        c = r["cellValuesByColumnId"]
        out.append({"id": r["id"], "name": (c.get(col["Company Name"]) or "").strip(),
                    "url": (c.get(col["Jobs Page"]) or {}).get("url"),
                    "employees": c.get(col["Employees"]), "country": c.get(col["HQ Country"]),
                    "city": c.get(col["HQ City"]), "swe_flag": bool(swe_sel & set(c.get(epd["id"]) or []))})
    return out


# ---- ATS detection ----------------------------------------------------------
ATS_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_board/js\?for=([\w-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)"),
    ("lever", r"api\.lever\.co/v0/postings/([\w.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([\w.%-]+)"),
    ("workday", r"([\w-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)"),
    ("recruitee", r"([\w-]+)\.recruitee\.com"),
    ("breezy", r"([\w-]+)\.breezy\.hr"),
    ("workable", r"apply\.workable\.com/([\w-]+)"),
    ("workable", r"(?<![\w.-])([\w-]+)\.workable\.com"),
    ("teamtailor", r"(https://[\w-]+\.teamtailor\.com)"),
    ("bamboohr", r"([\w-]+)\.bamboohr\.com"),
    ("rippling", r"ats\.rippling\.com/([\w-]+)"),
    ("rippling", r"([\w-]+)\.rippling-ats\.com"),
    ("gem", r"jobs\.gem\.com/([\w-]+)"),
]
BAD_SLUGS = {"embed", "jobs", "careers", "v1", "www", "api", "app", "attract", "resources", "job-seekers",
             "static", "assets", "cdn", "help", "support", "blog", "s", "en", "en-us", "job_board", "boards",
             "postings", "posting-api", "apply", "company", "about", "privacy", "home", "career", "job-boards",
             "jobs-boards", "hire", "join", "work", "talent", "team", "na", "eu", "cdn-assets", "support",
             "landing", "auth", "careers-analytics", "resources-workable", "j"}
UNSUPPORTED = re.compile(r"icims|jobvite|ultipro|ukg|teamtailor|applytojob|jazzhr|paylocity|adp\.com|"
                         r"successfactors|taleo|oraclecloud|personio|pinpoint|dover|wellfound|ycombinator")


def find_ats(text):
    seen = []
    for ats, pat in ATS_PATTERNS:
        for m in re.finditer(pat, text):
            slug = m.group(1).rstrip(".")
            if slug.lower() in BAD_SLUGS or (ats, slug) in seen:
                continue
            seen.append((ats, slug))
    return seen


def guesses(c):
    """ponytail: slug guessing for LinkedIn/dead links; can hit a same-named other company (marked 'guessed')."""
    name = re.sub(r"\(.*?\)|,? (inc|llc|ltd|corp|co)\.?$", "", c["name"], flags=re.I).strip().lower()
    s = {re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-")}
    m = re.search(r"linkedin\.com/company/([\w-]+)", c["url"] or "")
    if m:
        s.add(m.group(1).lower())
    m = re.match(r"https?://(?:www\.|careers\.|jobs\.)?([\w-]+)\.", c["url"] or "")
    if m and "linkedin" not in m.group(1):
        s.add(m.group(1).lower())
    return [(a, x) for x in s if x and x not in BAD_SLUGS for a in ("greenhouse", "ashby", "lever")]


# ---- ATS fetchers: each returns [{title, location, url, posted}] ------------
def j_greenhouse(slug):
    return [{"title": j["title"], "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"], "posted": j.get("first_published") or j.get("updated_at")}
            for j in get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")["jobs"]]


def j_lever(slug):
    out = []
    for j in get(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        out.append({"title": j["text"], "location": " / ".join(filter(None, locs + [j.get("country")])),
                    "url": j["hostedUrl"], "posted": j.get("createdAt")})
    return out


def j_ashby(slug):
    out = []
    for j in get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")["jobs"]:
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        out.append({"title": j["title"], "location": " / ".join(filter(None, locs + [country])),
                    "url": j["jobUrl"], "posted": j.get("publishedAt")})
    return out


def j_workday(spec):
    host, site = spec.split("/", 1)[0], spec.rsplit("/", 1)[1]
    tenant = host.split(".")[0]
    out, offset = [], 0
    while offset < 400:  # ponytail: cap 20 pages of "engineer" hits; huge tenants get truncated
        r = get(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                data={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "engineer developer"})
        posts = r.get("jobPostings") or []
        out += [{"title": j["title"], "location": j.get("locationsText", ""),
                 "url": f"https://{host}/{site}{j['externalPath']}", "posted": None} for j in posts]
        offset += 20
        if len(posts) < 20 or offset >= (r.get("total") or 0):
            break
    return out


def j_smartrecruiters(slug):
    out, offset = [], 0
    while True:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        for j in r["content"]:
            l = j.get("location") or {}
            out.append({"title": j["name"], "location": ", ".join(filter(None, [l.get("city"), l.get("region"),
                        l.get("country"), "Remote" if l.get("remote") else None])),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "posted": j.get("releasedDate")})
        offset += 100
        if offset >= r.get("totalFound", 0):
            return out


def j_recruitee(slug):
    return [{"title": j["title"], "location": ", ".join(filter(None, [j.get("location"), j.get("country")])),
             "url": j["careers_url"], "posted": j.get("published_at")}
            for j in get(f"{slug if slug.startswith('http') else f'https://{slug}.recruitee.com'}/api/offers/")["offers"]]


def j_breezy(slug):
    return [{"title": j["name"], "location": (j.get("location") or {}).get("name", ""), "url": j["url"],
             "posted": j.get("published_date")} for j in get(f"https://{slug}.breezy.hr/json")]


def j_workable(slug):
    return [{"title": j["title"], "location": ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])),
             "url": j["url"], "posted": j.get("published_on")}
            for j in get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")["jobs"]]


def j_bamboohr(slug):
    out = []
    for j in get(f"https://{slug}.bamboohr.com/careers/list")["result"]:
        l = j.get("atsLocation") or j.get("location") or {}
        out.append({"title": j["jobOpeningName"],
                    "location": ", ".join(filter(None, [l.get("city"), l.get("state"), l.get("province"),
                                                        l.get("country"), "Remote" if j.get("isRemote") else None])),
                    "url": f"https://{slug}.bamboohr.com/careers/{j['id']}", "posted": None})
    return out


def j_rippling(slug):
    return [{"title": j["name"].strip(), "location": " / ".join(
                f"{l.get('name', '')}, {l.get('countryCode', '')}" for l in j.get("locations") or []),
             "url": j["url"], "posted": None}
            for j in get(f"https://ats.rippling.com/api/v2/board/{slug}/jobs")["items"]]


def j_teamtailor(base):
    import xml.etree.ElementTree as ET
    req = urllib.request.Request(f"{base}/jobs.rss", headers={"User-Agent": UA})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=25).read())
    tt = "{https://teamtailor.com/locations}"
    out = []
    for it in root.iter("item"):
        locs = [", ".join(filter(None, [l.findtext(tt + "city"), l.findtext(tt + "name"), l.findtext(tt + "country")]))
                for l in it.iter(tt + "location")]
        if it.findtext("remoteStatus") in ("fully", "hybrid") and not locs:
            locs = ["Remote"]
        out.append({"title": it.findtext("title"), "location": " / ".join(locs), "url": it.findtext("link"),
                    "posted": it.findtext("pubDate")})
    return out


def j_gem(slug):
    return [{"title": j["title"].strip(), "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"], "posted": j.get("first_published_at")}
            for j in get(f"https://api.gem.com/job_board/v0/{slug}/job_posts/")]


FETCH = {k[2:]: v for k, v in globals().items() if k.startswith("j_")}


# ---- per-company pipeline ---------------------------------------------------
def scan(c):
    """Returns (status dict, jobs list). status.ok=True means board fetched, so missing jobs are truly closed."""
    url, st = c["url"] or "", {"id": c["id"], "name": c["name"], "url": c["url"], "country": c["country"],
                               "employees": c["employees"], "city": c.get("city"), "swe_flag": c.get("swe_flag")}
    cands, how = find_ats(url), "listed"
    if not cands and url and "linkedin.com" not in url:
        try:
            html, final = get(url, raw=True, headers={"Accept": "text/html"})
            cands, how = find_ats(final + " " + html), "page"
            base = re.match(r"https?://[^/]+", final).group(0)
            for ats in ("teamtailor", "recruitee"):  # custom-domain boards: API lives on the page's own host
                if ats in html and not any(a == ats for a, _ in cands):
                    cands.append((ats, base))
        except Exception as e:
            st["page_error"] = str(e)[:120]
    if not cands and UNSUPPORTED.search(url):
        st.update(status="unsupported", ats=UNSUPPORTED.search(url).group(0))
        return st, []
    tries = [(a, s, how) for a, s in cands] + [(a, s, "guessed") for a, s in guesses(c) if (a, s) not in cands]
    for ats, slug, how in tries:
        try:
            jobs = FETCH[ats](slug)
        except Exception as e:
            st.setdefault("errors", []).append(f"{ats}/{slug}: {str(e)[:80]}")
            continue
        if how == "guessed" and not jobs:
            continue
        st.update(status="ok", ats=ats, slug=slug, how=how, total=len(jobs))
        out = []
        for j in jobs:
            us = us_status(j["location"])
            if is_swe(j["title"]) and us != "no":
                out.append({**j, "us": us, "company": c["name"], "company_id": c["id"], "ats": ats})
        st["swe_us"] = len(out)
        return st, out
    st["status"] = "unresolved"
    return st, []


def norm_url(u):
    return re.sub(r"[?#].*$", "", u).rstrip("/").lower()


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    DOCS.mkdir(exist_ok=True)
    try:
        companies = load_companies()
    except Exception as e:  # Airtable hiccup: reuse last known list
        print("airtable failed, using cached list:", e, file=sys.stderr)
        companies = json.loads(COMPANIES_FILE.read_text())["companies"]
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    companies = companies[:limit]

    with ThreadPoolExecutor(24) as ex:
        results = list(ex.map(scan, companies))

    statuses, found, boards_seen = [], {}, set()
    for st, jobs in results:
        board = (st.get("ats"), st.get("slug"))
        if st.get("status") == "ok" and board in boards_seen:  # same board listed twice in the sheet
            st["status"], st["swe_us"] = "duplicate", 0
            statuses.append(st)
            continue
        boards_seen.add(board)
        statuses.append(st)
        for j in jobs:
            found.setdefault(norm_url(j["url"]), j)

    old = json.loads(JOBS_FILE.read_text())["jobs"] if JOBS_FILE.exists() else {}
    ok_ids = {s["id"] for s in statuses if s.get("status") == "ok"}
    merged, new = {}, 0
    for k, j in found.items():
        prev = old.get(k)
        if not prev or not prev.get("open"):
            new += prev is None
        merged[k] = {**j, "first_seen": prev["first_seen"] if prev else now, "open": True}
    cutoff = time.time() - CLOSED_TTL_DAYS * 86400
    for k, j in old.items():
        if k in merged:
            continue
        if j["company_id"] in ok_ids or j["company_id"] not in {c["id"] for c in companies}:
            j = {**j, "open": False, "closed_at": j.get("closed_at") or now}
        # else: board fetch failed this run -> keep previous state, don't falsely close
        if j.get("open") or datetime.fromisoformat(j["closed_at"].replace("Z", "+00:00")).timestamp() > cutoff:
            merged[k] = j

    JOBS_FILE.write_text(json.dumps({"updated": now, "jobs": merged}, indent=0, sort_keys=True))
    COMPANIES_FILE.write_text(json.dumps({"updated": now, "companies": statuses}, indent=0))
    by = {}
    for s in statuses:
        by[s.get("status")] = by.get(s.get("status"), 0) + 1
    print(f"companies={len(companies)} {by} open_swe_us={sum(j['open'] for j in merged.values())} new={new}")


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        assert is_swe("Senior Software Engineer, Backend") and is_swe("iOS Developer") and is_swe("Staff SWE")
        assert not is_swe("Sales Engineer") and not is_swe("Engineering Manager") and not is_swe("Solutions Engineer")
        assert not is_swe("Account Executive") and not is_swe("Sr. Power Electronics Engineer - TWT (Starlink)")
        assert not is_swe("Developer Engagement Representative - LATAM") and not is_swe("Mission Engineer II")
        assert is_swe("Principal Engineer, Studio Builder Tools") and is_swe("Senior Engineer") and is_swe("Staff Engineer II")
        assert is_swe("Backend Engineer (Multiple Positions)") and is_swe("Machine Learning Engineer")
        assert us_status("San Francisco, CA") == "yes" and us_status("Remote - US") == "yes"
        assert us_status("New York, United States") == "yes" and us_status("Austin, Texas") == "yes"
        assert us_status("Toronto, Canada") == "no" and us_status("London") == "no"
        assert us_status("Remote") == "maybe" and us_status("") == "maybe"
        assert us_status("London / New York") == "yes" and us_status("Berlin, DE") == "no"
        assert us_status("Pune, IN") == "no" and us_status("Remote, US") == "yes" and us_status("Plano, TX") == "yes"
        assert find_ats("https://job-boards.greenhouse.io/launchdarkly") == [("greenhouse", "launchdarkly")]
        assert find_ats("x jobs.lever.co/acme/abc y") == [("lever", "acme")]
        assert find_ats("https://zyte.workable.com/j/1 www.workable.com") == [("workable", "zyte")]
        assert find_ats("https://genesys.wd1.myworkdayjobs.com/en-US/Genesys")[0] == ("workday", "genesys.wd1.myworkdayjobs.com/en-US/Genesys")
        print("ok")
    else:
        main()
