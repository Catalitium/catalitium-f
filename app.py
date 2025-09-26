# app.py
import os, csv, re, sqlite3
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, g

# --- Config ------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.getenv("DB_PATH", os.path.join(BASE_DIR, "catalitium.db"))
JOBS_CSV   = os.getenv("JOBS_CSV",  os.path.join(BASE_DIR, "jobs.csv"))        # TAB by default
SALARY_CSV = os.getenv("SALARY_CSV", os.path.join(BASE_DIR, "salary.csv"))     # reference ranges
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
GTM_CONTAINER_ID = os.getenv("GTM_CONTAINER_ID", "GTM-MNJ9SSL9")

# Hard cap
PER_PAGE_MAX = 100

app = Flask(__name__, template_folder="templates")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    DB_PATH=DB_PATH,
    GTM_CONTAINER_ID=GTM_CONTAINER_ID,
    TEMPLATES_AUTO_RELOAD=True,
)

@app.context_processor
def inject_globals():
    return {"gtm_container_id": app.config.get("GTM_CONTAINER_ID")}

# --- SQLite: subscribers + search_logs ---------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS subscribers (
        email TEXT UNIQUE,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS search_logs (
        term TEXT,
        country TEXT,
        created_at TEXT
    );
    """)
    db.commit()

@app.before_request
def _ensure_db():
    init_db()

# --- Helpers -----------------------------------------------------------------
def _now_iso():
    # timezone-aware UTC
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _valid_email(email):
    return bool(email and "@" in email and "." in email and 3 <= len(email) <= 254)

COUNTRY_NORM = {
    "deutschland":"DE","germany":"DE","deu":"DE","de":"DE",
    "switzerland":"CH","schweiz":"CH","suisse":"CH","svizzera":"CH","ch":"CH",
    "austria":"AT","österreich":"AT","at":"AT",
    "europe":"EU","eu":"EU",
    "uk":"UK","gb":"UK","england":"UK","united kingdom":"UK",
    "usa":"US","united states":"US","america":"US","us":"US",
    "spain":"ES","es":"ES","france":"FR","fr":"FR","italy":"IT","it":"IT",
    "netherlands":"NL","nl":"NL","belgium":"BE","be":"BE","sweden":"SE","se":"SE",
}

TITLE_SYNONYMS = {
    "swe":"software engineer","software eng":"software engineer","sw eng":"software engineer",
    "frontend":"front end","front-end":"front end","backend":"back end","back-end":"back end",
    "fullstack":"full stack","full-stack":"full stack",
    "pm":"product manager","prod mgr":"product manager","product owner":"product manager",
    "ds":"data scientist","ml":"machine learning","mle":"machine learning engineer",
    "sre":"site reliability engineer","devops":"devops","sec eng":"security engineer","infosec":"security",
}

def normalize_country(q: str) -> str:
    if not q: return ""
    t = q.strip().lower()
    if t in COUNTRY_NORM: return COUNTRY_NORM[t]
    if len(t) == 2 and t.isalpha(): return t.upper()
    for token, code in COUNTRY_NORM.items():
        if token in t: return code
    return q.strip()

def normalize_title(q: str) -> str:
    if not q: return ""
    s = q.lower()
    for k, v in TITLE_SYNONYMS.items():
        if k in s: s = s.replace(k, v)
    s = re.sub(r"[^\w\s\-\/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_country_code_from_location(loc: str) -> str:
    if not loc: return ""
    parts = re.split(r"[^A-Za-z0-9]+", loc)
    for token in reversed([p for p in parts if p]):
        t = token.lower()
        if t in COUNTRY_NORM: return COUNTRY_NORM[t]
        if len(t) == 2 and t.isalpha(): return t.upper()
    return ""

def parse_money_numbers(text: str):
    if not text: return []
    nums = []
    for raw in re.findall(r'(?i)\d[\d,.\s]*k?+', text):
        clean = raw.lower().replace(",", "").replace(" ", "")
        mult = 1000 if clean.endswith("k") else 1
        clean = clean.rstrip("k").replace(".", "")
        if clean.isdigit():
            nums.append(int(clean) * mult)
    return nums

def parse_salary_range_from_text(text: str):
    nums = parse_money_numbers(text)
    if not nums: return (None, None)
    return (min(nums), max(nums) if len(nums) > 1 else None)

def parse_salary_query(q: str):
    if not q: return ("", None, None)
    s = q.strip()
    m = re.search(r'(?i)(\d[\d,.\s]*k?)\s*[-–]\s*(\d[\d,.\s]*k?)', s)
    if m:
        low = parse_money_numbers(m.group(1))
        high = parse_money_numbers(m.group(2))
        s_clean = (s[:m.start()] + s[m.end():]).strip()
        return (s_clean, low[0] if low else None, high[-1] if high else None)
    m = re.search(r'(?i)>\s*=?\s*(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        s_clean = (s[:m.start()] + s[m.end():]).strip()
        return (s_clean, v[0] if v else None, None)
    m = re.search(r'(?i)<\s*=?\s*(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        s_clean = (s[:m.start()] + s[m.end():]).strip()
        return (s_clean, None, v[0] if v else None)
    m = re.search(r'(?i)(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        s_clean = (s[:m.start()] + s[m.end():]).strip()
        return (s_clean, v[0] if v else None, None)
    return (s, None, None)

# --- CSV helpers --------------------------------------------------------------
def _sniff_reader(fp, default_delim="\t"):
    sample = fp.read(4096)
    fp.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
    except Exception:
        class _D: delimiter = default_delim
        dialect = _D()
    return csv.DictReader(fp, dialect=dialect)

_salary_cache = {"path": None, "mtime": 0, "map": {}}

# --- Salary CSV Reader & Enrichment ------------------------------------------
_salary_cache = {"path": None, "mtime": 0, "map": {}}

def read_salary_reference():
    """
    Reads salary.csv (with headers: GeoSalaryId, Location, MedianSalary, MinSalary,
    CurrencyTicker, City, Country, Region, RemoteType) and returns a dict keyed
    by (city.lower(), country.lower()) and (None, country.lower()).
    """
    path = SALARY_CSV
    if not os.path.exists(path):
        return {}

    mtime = os.path.getmtime(path)
    if _salary_cache["path"] == path and _salary_cache["mtime"] == mtime:
        return _salary_cache["map"]

    ref = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t" if path.endswith(".tsv") else ",")
        for row in reader:
            city = (row.get("City") or "").strip().lower()
            country = (row.get("Country") or "").strip().lower()
            currency = (row.get("CurrencyTicker") or "").strip().upper()
            median = row.get("MedianSalary")
            minval = row.get("MinSalary")

            try:
                median = int(float(median))
            except Exception:
                median = None
            try:
                minval = int(float(minval))
            except Exception:
                minval = None

            if not country:
                continue

            key_city = (city, country)
            key_country = (None, country)

            ref[key_city] = {
                "median": median,
                "min": minval,
                "currency": currency,
                "label": row.get("City") or row.get("Country"),
            }
            # fallback country-only entry
            ref.setdefault(key_country, {
                "median": median,
                "min": minval,
                "currency": currency,
                "label": row.get("Country"),
            })

    _salary_cache.update({"path": path, "mtime": mtime, "map": ref})
    return ref


def enrich_with_salary_reference(rows):
    """
    For each job in rows, attach reference salary info:
      - ref_median
      - ref_min
      - ref_currency
      - ref_match_label
    Preference order: (City+Country) → (Country)
    """
    ref_map = read_salary_reference()
    if not ref_map:
        return rows

    for j in rows:
        city = (j.get("City") or "").strip().lower()
        country = (j.get("Country") or "").strip().lower()

        ref = None
        if (city, country) in ref_map:
            ref = ref_map[(city, country)]
        elif (None, country) in ref_map:
            ref = ref_map[(None, country)]

        if ref:
            j["ref_median"] = ref["median"]
            j["ref_min"] = ref["min"]
            j["ref_currency"] = ref["currency"]
            j["ref_match_label"] = ref["label"]

    return rows


def _match_salary_ref(n_title: str, location_text: str, country_code: str, ref_map: dict):
    if location_text:
        n_loc = normalize_title(location_text)
        val = ref_map.get((n_title, f"L:{n_loc}"))
        if val: return (*val, location_text)
    if country_code:
        val = ref_map.get((n_title, f"C:{country_code.upper()}"))
        if val: return (*val, country_code.upper())
    val = ref_map.get((n_title, "*"))
    if val: return (*val, "Global")
    return (None, None, None)

def read_jobs_csv():
    if not os.path.exists(JOBS_CSV): return []
    jobs = []
    with open(JOBS_CSV, newline="", encoding="utf-8", errors="replace") as f:
        reader = _sniff_reader(f, default_delim="\t")
        for i, row in enumerate(reader, start=1):
            title = (row.get("JobTitle") or row.get("Title") or "").strip()
            company = (row.get("CompanyName") or row.get("Company") or "").strip()
            city = (row.get("City") or "").strip()
            country_raw = (row.get("Country") or "").strip()
            location = (row.get("Location") or "").strip() or ", ".join([p for p in [city, country_raw] if p]) or "Remote"
            desc = (row.get("Description") or row.get("Summary") or row.get("NormalizedJob") or "").strip() or title
            date_posted = (row.get("CreatedAt") or row.get("DatePosted") or "").strip()
            salary_text = (row.get("Salary") or "").strip()
            smin, smax = parse_salary_range_from_text(salary_text)
            if not title and not company: continue
            code = extract_country_code_from_location(location) or normalize_country(country_raw)
            jobs.append({
                "id": (row.get("JobID") or row.get("Id") or str(i)).strip(),
                "title": title or "(Untitled)",
                "company": company or "—",
                "location": location,
                "description": desc,
                "date_posted": date_posted[:10] if date_posted else "",
                "salary_min": smin,
                "salary_max": smax,
                "country_code": code or "",
            })
    return jobs

def job_effective_salary_range(j):
    if j.get("salary_min") or j.get("salary_max"):
        return (j.get("salary_min"), j.get("salary_max"))
    if j.get("ref_salary_min") or j.get("ref_salary_max"):
        return (j.get("ref_salary_min"), j.get("ref_salary_max"))
    return (None, None)

def _tokens(text): return [t for t in re.split(r"[^\w+]+", text.lower()) if t]

def _fuzzy_match(needle: str, hay: str) -> bool:
    if not needle: return True
    n_tokens = _tokens(needle)
    hay_l = hay.lower()
    return all(tok in hay_l for tok in n_tokens)

def filter_jobs(rows, title_q, country_q, sal_min_req=None, sal_max_req=None):
    tq = normalize_title(title_q or "")
    cq = normalize_country(country_q or "")
    out = []
    for r in rows:
        text = (r["title"] + " " + r["company"] + " " + r["description"])
        loc  = r["location"]
        ok = True
        if tq and not _fuzzy_match(tq, text): ok = False
        if ok and cq and cq.lower() not in loc.lower(): ok = False
        if ok and (sal_min_req is not None or sal_max_req is not None):
            jmin, jmax = job_effective_salary_range(r)
            if jmin is None and jmax is None: ok = False
            else:
                if jmin is None: jmin = 0
                if jmax is None: jmax = max(jmin, jmax or jmin)
                if sal_min_req is not None and jmax < sal_min_req: ok = False
                if sal_max_req is not None and jmin > sal_max_req: ok = False
        if ok: out.append(r)
    return out

def log_search(term, country):
    if not term and not country: return
    db = get_db()
    db.execute("INSERT INTO search_logs(term,country,created_at) VALUES(?,?,?)",
               (term or "", country or "", _now_iso()))
    db.commit()

def paginate(items, page, per_page):
    total = len(items)
    page = 1 if page < 1 else page
    per_page = min(max(1, per_page), PER_PAGE_MAX)
    start = (page - 1) * per_page
    end = start + per_page
    sliced = items[start:end]
    pages = (total + per_page - 1) // per_page
    return {
        "items": sliced,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }

# --- Routes ------------------------------------------------------------------
@app.get("/")
def index():
    raw_title = (request.args.get("title") or "").strip()
    raw_country = (request.args.get("country") or "").strip()
    page = int(request.args.get("page", 1) or 1)
    per_page_req = int(request.args.get("per_page", PER_PAGE_MAX) or PER_PAGE_MAX)

    cleaned_title, sal_floor, sal_ceiling = parse_salary_query(raw_title)
    title_q = normalize_title(cleaned_title)
    country_q = normalize_country(raw_country)

    rows = read_jobs_csv()
    rows = enrich_with_salary_reference(rows)
    filtered = filter_jobs(rows, title_q, country_q, sal_floor, sal_ceiling)

    if raw_title or raw_country:
        log_search(raw_title, raw_country)

    # Pagination
    pg = paginate(filtered, page, per_page_req)
    for r in pg["items"]:
        r.pop("country_code", None)

    # Build pagination URLs
    def _url(p):
        return url_for("index", title=title_q or None, country=country_q or None, page=p, per_page=pg["per_page"])

    pagination = {
        "page": pg["page"],
        "pages": pg["pages"],
        "total": pg["total"],
        "per_page": pg["per_page"],
        "has_prev": pg["has_prev"],
        "has_next": pg["has_next"],
        "prev_url": _url(pg["page"] - 1) if pg["has_prev"] else None,
        "next_url": _url(pg["page"] + 1) if pg["has_next"] else None,
    }

    return render_template("index.html",
                           results=pg["items"],
                           count=pg["total"],
                           title_q=title_q,
                           country_q=country_q,
                           pagination=pagination)

@app.post("/subscribe")
def subscribe():
    email = (request.form.get("email") or "").strip()
    if not _valid_email(email):
        flash("Please enter a valid email.", "error")
        return redirect(url_for("index"))
    db = get_db()
    try:
        db.execute("INSERT INTO subscribers(email, created_at) VALUES(?, ?)", (email, _now_iso()))
        db.commit()
        flash("You're subscribed! 🎉", "success")
    except sqlite3.IntegrityError:
        flash("You're already on the list. 👍", "success")
    return redirect(url_for("index"))

# --- Main --------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
