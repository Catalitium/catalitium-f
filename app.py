# app.py — Catalitium (Render-ready, gunicorn entrypoint: app:app)
import os, csv, re, sqlite3, logging
import contextlib
try:
    import psycopg  # psycopg v3
except Exception:
    psycopg = None  # optional, only required when SUPABASE_URL is set
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, g, jsonify
import hashlib, uuid

# ------------------------- Config --------------------------------------------
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# Configure storage backend (default: memory://)
RATELIMIT_STORAGE_URL = os.getenv("RATELIMIT_STORAGE_URL", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=RATELIMIT_STORAGE_URL,
    app=app
)


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from email_validator import validate_email, EmailNotValidError  # RFC compliant
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.getenv("DB_PATH",     os.path.join(BASE_DIR, "catalitium.db"))
# Prefer DATABASE_URL for Postgres; fallback to SUPABASE_URL for backwards-compat
SUPABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_URL") or "").strip()
# Enforce sslmode=require in URL if using Postgres and not present
if SUPABASE_URL.startswith("postgres") and "sslmode=" not in SUPABASE_URL:
    SUPABASE_URL += ("&" if "?" in SUPABASE_URL else "?") + "sslmode=require"
JOBS_ENCRYPTED = os.getenv("JOBS_ENCRYPTED", "0").strip() in ("1", "true", "yes")
SALARY_ENCRYPTED = os.getenv("SALARY_ENCRYPTED", "0").strip() in ("1", "true", "yes")
DATA_ENC_KEY = os.getenv("DATA_ENC_KEY", "").strip()  # base64 urlsafe key for Fernet
# Require explicit env paths; avoid shipping sensitive CSVs by default
JOBS_CSV    = os.getenv("JOBS_CSV",    "").strip()
SALARY_CSV  = os.getenv("SALARY_CSV",  "").strip()
SECRET_KEY  = os.getenv("SECRET_KEY",  "").strip()
GTM_ID      = os.getenv("GTM_CONTAINER_ID", "GTM-MNJ9SSL9")
PER_PAGE_MAX = 100  # safety cap

app = Flask(__name__, template_folder="templates")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    DB_PATH=DB_PATH,
    GTM_CONTAINER_ID=GTM_ID,
    TEMPLATES_AUTO_RELOAD=False,  # production default
    DB_BACKEND=("postgres" if SUPABASE_URL else "sqlite"),
)

# Rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"])  # global sane default

# ------------------------- Logging -------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("catalitium")

# Enforce SECRET_KEY always (safer default)
if not SECRET_KEY or SECRET_KEY == "dev-insecure-change-me":
    logger.error("SECRET_KEY must be set via environment. Aborting.")
    raise SystemExit(1)

@app.context_processor
def inject_globals():
    return {"gtm_container_id": app.config.get("GTM_CONTAINER_ID")}

# ------------------------- Database (SQLite or Postgres) ----------------------
def _pg_connect():
    if not SUPABASE_URL:
        return None
    if psycopg is None:
        raise RuntimeError("psycopg is required when SUPABASE_URL is set")
    # Enforce SSL for Supabase; autocommit=True for simple writes
    conn = psycopg.connect(SUPABASE_URL, autocommit=True, sslmode="require")
    return conn

def get_db():
    if "db" in g:
        return g.db
    if app.config["DB_BACKEND"] == "postgres":
        g.db = _pg_connect()
        return g.db
    # default: sqlite
    g.db = sqlite3.connect(app.config["DB_PATH"])
    g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if not db:
        return
    with contextlib.suppress(Exception):
        db.close()

def init_db():
    db = get_db()
    if app.config["DB_BACKEND"] == "postgres":
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    email TEXT UNIQUE,
                    created_at TIMESTAMP
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_logs (
                    term TEXT,
                    country TEXT,
                    created_at TIMESTAMP
                );
                """
            )
            # Analytics tables (Postgres)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    raw_title TEXT,
                    raw_country TEXT,
                    norm_title TEXT,
                    norm_country TEXT,
                    sal_floor INTEGER,
                    sal_ceiling INTEGER,
                    result_count INTEGER,
                    page INTEGER,
                    per_page INTEGER,
                    user_agent TEXT,
                    referer TEXT,
                    ip_hash TEXT,
                    session_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_search_events_created ON search_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_search_events_title ON search_events(norm_title);
                CREATE INDEX IF NOT EXISTS idx_search_events_country ON search_events(norm_country);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS job_view_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    job_id TEXT,
                    job_title TEXT,
                    company TEXT,
                    location TEXT,
                    norm_country TEXT,
                    user_agent TEXT,
                    ip_hash TEXT,
                    session_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_job_view_created ON job_view_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_job_view_country ON job_view_events(norm_country);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribe_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    email_hash TEXT,
                    status TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_subscribe_created ON subscribe_events(created_at);
                """
            )
        return
    # sqlite
    schema = """
    CREATE TABLE IF NOT EXISTS subscribers (
        email TEXT UNIQUE,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS search_logs (
        term TEXT,
        country TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS search_events (
        id INTEGER PRIMARY KEY,
        created_at TEXT,
        raw_title TEXT,
        raw_country TEXT,
        norm_title TEXT,
        norm_country TEXT,
        sal_floor INTEGER,
        sal_ceiling INTEGER,
        result_count INTEGER,
        page INTEGER,
        per_page INTEGER,
        user_agent TEXT,
        referer TEXT,
        ip_hash TEXT,
        session_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_search_events_created ON search_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_search_events_title ON search_events(norm_title);
    CREATE INDEX IF NOT EXISTS idx_search_events_country ON search_events(norm_country);
    CREATE TABLE IF NOT EXISTS job_view_events (
        id INTEGER PRIMARY KEY,
        created_at TEXT,
        job_id TEXT,
        job_title TEXT,
        company TEXT,
        location TEXT,
        norm_country TEXT,
        user_agent TEXT,
        ip_hash TEXT,
        session_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_job_view_created ON job_view_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_job_view_country ON job_view_events(norm_country);
    CREATE TABLE IF NOT EXISTS subscribe_events (
        id INTEGER PRIMARY KEY,
        created_at TEXT,
        email_hash TEXT,
        status TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_subscribe_created ON subscribe_events(created_at);
    """
    db.executescript(schema)
    db.commit()

_db_inited_once = False

@app.before_request
def _ensure_db():
    global _db_inited_once
    if not _db_inited_once:
        init_db()
        _db_inited_once = True
    # ensure session id cookie exists
    _ensure_sid()

def _hash(value: str) -> str:
    salt = os.getenv('ANALYTICS_SALT','dev')
    v = (value or '').encode('utf-8')
    return hashlib.sha256(salt.encode('utf-8') + v).hexdigest()

def _ensure_sid(resp=None):
    sid = request.cookies.get('sid')
    if not sid:
        sid = uuid.uuid4().hex
        if resp is None:
            @app.after_request
            def _set_sid(r):
                r.set_cookie('sid', sid, max_age=31536000, httponly=True, samesite='Lax', secure=request.is_secure)
                return r
        else:
            resp.set_cookie('sid', sid, max_age=31536000, httponly=True, samesite='Lax', secure=request.is_secure)
    return sid

def _client_meta():
    ua = request.headers.get('User-Agent','')
    ref = request.headers.get('Referer','')
    xff = request.headers.get('X-Forwarded-For', '')
    ip = (xff.split(',')[0].strip() if xff else (request.remote_addr or ''))
    return ua, ref, _hash(ip), request.cookies.get('sid') or ''

def log_search_event(raw_title, raw_country, norm_title, norm_country, sal_floor, sal_ceiling, result_count, page, per_page):
    ua, ref, ip_h, sid = _client_meta()
    db = get_db()
    if app.config["DB_BACKEND"] == "postgres":
        with db.cursor() as cur:
            cur.execute(
                'INSERT INTO search_events(created_at,raw_title,raw_country,norm_title,norm_country,sal_floor,sal_ceiling,result_count,page,per_page,user_agent,referer,ip_hash,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (_now_iso(), raw_title or '', raw_country or '', norm_title or '', norm_country or '', sal_floor, sal_ceiling, result_count, page, per_page, ua[:300], ref[:300], ip_h, sid)
            )
        return
    db.execute('INSERT INTO search_events(created_at,raw_title,raw_country,norm_title,norm_country,sal_floor,sal_ceiling,result_count,page,per_page,user_agent,referer,ip_hash,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (_now_iso(), raw_title or '', raw_country or '', norm_title or '', norm_country or '', sal_floor, sal_ceiling, result_count, page, per_page, ua[:300], ref[:300], ip_h, sid))
    db.commit()

def log_job_view_event(job_id, job_title, company, location, norm_country):
    ua, ref, ip_h, sid = _client_meta()
    db = get_db()
    if app.config["DB_BACKEND"] == "postgres":
        with db.cursor() as cur:
            cur.execute(
                'INSERT INTO job_view_events(created_at,job_id,job_title,company,location,norm_country,user_agent,ip_hash,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (_now_iso(), str(job_id or ''), job_title or '', company or '', location or '', norm_country or '', ua[:300], ip_h, sid)
            )
        return
    db.execute('INSERT INTO job_view_events(created_at,job_id,job_title,company,location,norm_country,user_agent,ip_hash,session_id) VALUES(?,?,?,?,?,?,?,?,?)', (_now_iso(), str(job_id or ''), job_title or '', company or '', location or '', norm_country or '', ua[:300], ip_h, sid))
    db.commit()

# ------------------------- Helper utils --------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# _valid_email replaced by email_validator library in /subscribe

def _tokens(text: str):
    return [t for t in re.split(r"[^\w+]+", text.lower()) if t]

def _fuzzy_match(needle: str, hay: str) -> bool:
    """Loose containment check for tokens (case insensitive)."""
    if not needle:
        return True
    n_tokens = _tokens(needle)
    hay_l = hay.lower()
    return all(tok in hay_l for tok in n_tokens)

# ------------------------- Normalization dictionaries ------------------------
COUNTRY_NORM = {
    "deutschland":"DE","germany":"DE","deu":"DE","de":"DE",
    "switzerland":"CH","schweiz":"CH","suisse":"CH","svizzera":"CH","ch":"CH",
    "austria":"AT","österreich":"AT","at":"AT",
    "europe":"EU","eu":"EU",
    "uk":"UK","gb":"UK","england":"UK","united kingdom":"UK",
    "usa":"US","united states":"US","america":"US","us":"US",
    "spain":"ES","es":"ES","france":"FR","fr":"FR","italy":"IT","it":"IT",
    "netherlands":"NL","nl":"NL","belgium":"BE","be":"BE","sweden":"SE","se":"SE",
    "poland":"PL","colombia":"CO","mexico":"MX",
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
    """Return normalized country code if possible."""
    if not q:
        return ""
    t = q.strip().lower()
    if t in COUNTRY_NORM:
        return COUNTRY_NORM[t]
    if len(t) == 2 and t.isalpha():
        return t.upper()
    for token, code in COUNTRY_NORM.items():
        if token in t:
            return code
    return q.strip()

def extract_country_code(loc: str, country_fallback: str = "") -> str:
    """Try to extract country code from location string or fallback country."""
    if loc:
        parts = re.split(r"[^A-Za-z0-9]+", loc)
        for token in reversed([p for p in parts if p]):
            t = token.lower()
            if t in COUNTRY_NORM:
                return COUNTRY_NORM[t]
            if len(t) == 2 and t.isalpha():
                return t.upper()
    return normalize_country(country_fallback)

def normalize_title(q: str) -> str:
    if not q:
        return ""
    s = q.lower()
    for k, v in TITLE_SYNONYMS.items():
        if k in s:
            s = s.replace(k, v)
    s = re.sub(r"[^\w\s\-\/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ------------------------- Salary parsing ------------------------------------
def parse_money_numbers(text: str):
    if not text:
        return []
    nums = []
    for raw in re.findall(r'(?i)\d[\d,.\s]*k?', text):
        clean = raw.lower().replace(",", "").replace(" ", "")
        mult = 1000 if clean.endswith("k") else 1
        clean = clean.rstrip("k").replace(".", "")
        if clean.isdigit():
            nums.append(int(clean) * mult)
    return nums

def parse_salary_range_from_text(text: str):
    nums = parse_money_numbers(text)
    if not nums:
        return (None, None)
    return (min(nums), max(nums) if len(nums) > 1 else None)

def parse_salary_query(q: str):
    """Parse inline salary filters like '80k-120k', '>100k', '<=90k', '120k'."""
    if not q:
        return ("", None, None)
    s = q.strip()

    # Range (80k-120k)
    m = re.search(r'(?i)(\d[\d,.\s]*k?)\s*[-–]\s*(\d[\d,.\s]*k?)', s)
    if m:
        low = parse_money_numbers(m.group(1))
        high = parse_money_numbers(m.group(2))
        return (s[:m.start()] + s[m.end():]).strip(), low[0] if low else None, high[-1] if high else None

    # Greater than
    m = re.search(r'(?i)>\s*=?\s*(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        return (s[:m.start()] + s[m.end():]).strip(), v[0] if v else None, None

    # Less than
    m = re.search(r'(?i)<\s*=?\s*(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        return (s[:m.start()] + s[m.end():]).strip(), None, v[0] if v else None

    # Single number
    m = re.search(r'(?i)(\d[\d,.\s]*k?)', s)
    if m:
        v = parse_money_numbers(m.group(1))
        return (s[:m.start()] + s[m.end():]).strip(), v[0] if v else None, None

    return (s, None, None)

# ------------------------- CSV helpers ---------------------------------------
def _sniff_reader(fp, default_delim="\t"):
    sample = fp.read(4096)
    fp.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
    except Exception:
        class _D: delimiter = default_delim
        dialect = _D()
    return csv.DictReader(fp, dialect=dialect)

# ------------------------- Salary reference cache ----------------------------
_salary_cache = {"path": None, "mtime": 0, "map": {}}

def _maybe_decrypt_bytes(data: bytes) -> bytes:
    if not DATA_ENC_KEY:
        return data
    try:
        f = Fernet(DATA_ENC_KEY)
        return f.decrypt(data)
    except (InvalidToken, ValueError, TypeError):
        return data

def _open_csv_maybe_encrypted(path: str, encrypted_flag: bool):
    if not encrypted_flag:
        return open(path, "rb")
    # read and decrypt all at once, return a BytesIO-like wrapper for csv reader via text decoding
    import io
    raw = b""
    with open(path, "rb") as f:
        raw = f.read()
    dec = _maybe_decrypt_bytes(raw)
    return io.BytesIO(dec)

def read_salary_reference():
    path = SALARY_CSV
    if not os.path.exists(path):
        return {}

    mtime = os.path.getmtime(path)
    if _salary_cache["path"] == path and _salary_cache["mtime"] == mtime:
        return _salary_cache["map"]

    ref = {}
    # Support optional encryption; decode to text for csv
    fobj = _open_csv_maybe_encrypted(path, SALARY_ENCRYPTED)
    import io
    text_stream = io.TextIOWrapper(fobj, encoding="utf-8", errors="replace")
    with text_stream as f:
        reader = csv.DictReader(f, delimiter="\t" if path.lower().endswith((".tsv", ".tab")) else ",")
        for row in reader:
            city = (row.get("City") or "").strip().lower()
            country = (row.get("Country") or "").strip().lower()
            currency = (row.get("CurrencyTicker") or "").strip().upper()
            median = row.get("MedianSalary")
            minval = row.get("MinSalary")

            try: median = int(float(median)) if median else None
            except: median = None
            try: minval = int(float(minval)) if minval else None
            except: minval = None

            if not country:
                continue

            key_city = (city, country)
            key_country = (None, country)

            ref[key_city] = {
                "median": median,
                "min": minval,
                "currency": currency or "USD",
                "label": row.get("City") or row.get("Country") or "",
            }
            ref.setdefault(key_country, ref[key_city])

    _salary_cache.update({"path": path, "mtime": mtime, "map": ref})
    return ref

def enrich_with_salary_reference(rows):
    ref_map = read_salary_reference()
    if not ref_map:
        return rows

    for j in rows:
        city = (j.get("City") or "").strip().lower()
        country = (j.get("Country") or "").strip().lower()
        ref = ref_map.get((city, country)) or ref_map.get((None, country))
        if ref:
            j.update({
                "ref_median": ref["median"],
                "ref_min": ref["min"],
                "ref_currency": ref["currency"],
                "ref_match_label": ref["label"],
                "ref_salary_min": ref["min"],
                "ref_salary_max": ref["median"],
            })
    return rows

# ------------------------- Jobs CSV with cache -------------------------------
_jobs_cache = {"path": None, "mtime": 0, "rows": []}

def read_jobs_csv():
    path = JOBS_CSV
    if not os.path.exists(path):
        return []

    mtime = os.path.getmtime(path)
    if _jobs_cache["path"] == path and _jobs_cache["mtime"] == mtime:
        return _jobs_cache["rows"]

    jobs = []
    fobj = _open_csv_maybe_encrypted(path, JOBS_ENCRYPTED)
    import io
    text_stream = io.TextIOWrapper(fobj, encoding="utf-8", errors="replace")
    with text_stream as f:
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

            if not title and not company:
                continue

            code = extract_country_code(location, country_raw)
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
                "City": city,
                "Country": country_raw,
            })

    _jobs_cache.update({"path": path, "mtime": mtime, "rows": jobs})
    return jobs

# ------------------------- Filtering / Pagination ----------------------------
def job_effective_salary_range(j):
    return (
        j.get("salary_min") or j.get("ref_salary_min"),
        j.get("salary_max") or j.get("ref_salary_max"),
    )

def filter_jobs(rows, title_q, country_q, sal_min_req=None, sal_max_req=None):
    tq = normalize_title(title_q or "")
    cq = normalize_country(country_q or "")
    out = []
    for r in rows:
        text = f"{r['title']} {r['company']} {r['description']}"
        ok = True
        if tq and not _fuzzy_match(tq, text): ok = False
        if ok and cq and cq.lower() not in r["location"].lower(): ok = False
        if ok and (sal_min_req is not None or sal_max_req is not None):
            jmin, jmax = job_effective_salary_range(r)
            if jmin is None and jmax is None: ok = False
            else:
                jmin = jmin or 0
                jmax = jmax or jmin
                if sal_min_req is not None and jmax < sal_min_req: ok = False
                if sal_max_req is not None and jmin > sal_max_req: ok = False
        if ok: out.append(r)
    return out

def log_search(term, country):
    if not term and not country:
        return
    db = get_db()
    if app.config["DB_BACKEND"] == "postgres":
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO search_logs(term, country, created_at) VALUES(%s, %s, %s)",
                (term or "", country or "", _now_iso()),
            )
        return
    db.execute(
        "INSERT INTO search_logs(term,country,created_at) VALUES(?,?,?)",
        (term or "", country or "", _now_iso()),
    )
    db.commit()

def paginate(items, page, per_page):
    total = len(items)
    page = max(1, page)
    per_page = min(max(1, per_page), PER_PAGE_MAX)
    start, end = (page - 1) * per_page, page * per_page
    pages = (total + per_page - 1) // per_page
    return {
        "items": items[start:end],
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }

# ------------------------- Routes --------------------------------------------
@app.get("/")
def index():
    raw_title = (request.args.get("title") or "").strip()
    raw_country = (request.args.get("country") or "").strip()
    page = int(request.args.get("page", 1) or 1)
    per_page_req = int(request.args.get("per_page", PER_PAGE_MAX) or PER_PAGE_MAX)

    cleaned_title, sal_floor, sal_ceiling = parse_salary_query(raw_title)
    title_q = normalize_title(cleaned_title)
    country_q = normalize_country(raw_country)

    rows = enrich_with_salary_reference(read_jobs_csv())
    filtered = filter_jobs(rows, title_q, country_q, sal_floor, sal_ceiling)
    pg = paginate(filtered, page, per_page_req)

    if raw_title or raw_country:
        log_search(raw_title, raw_country)
        log_search_event(raw_title, raw_country, title_q, country_q, sal_floor, sal_ceiling, pg["total"], page, pg["per_page"])
    for r in pg["items"]:
        r.pop("country_code", None)

    def _url(p):
        return url_for(
            "index", title=title_q or None, country=country_q or None,
            page=p, per_page=pg["per_page"]
        )

    pagination = {
        "page": pg["page"], "pages": pg["pages"], "total": pg["total"],
        "per_page": pg["per_page"], "has_prev": pg["has_prev"],
        "has_next": pg["has_next"],
        "prev_url": _url(pg["page"] - 1) if pg["has_prev"] else None,
        "next_url": _url(pg["page"] + 1) if pg["has_next"] else None,
    }

    return render_template(
        "index.html",
        results=pg["items"], count=pg["total"],
        title_q=title_q, country_q=country_q, pagination=pagination,
    )

@app.post("/subscribe")
@limiter.limit("5/minute; 50/hour")
def subscribe():
    email = (request.form.get("email") or "").strip()
    # RFC-compliant validation
    try:
        email = validate_email(email, check_deliverability=False).normalized
    except EmailNotValidError:
        flash("Please enter a valid email.", "error")
        return redirect(url_for("index"))
    db = get_db()
    if app.config["DB_BACKEND"] == "postgres":
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscribers(email, created_at) VALUES(%s, %s)",
                    (email, _now_iso()),
                )
            flash("You're subscribed! 🎉", "success")
            # analytics
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscribe_events(created_at, email_hash, status) VALUES(%s,%s,%s)",
                    (_now_iso(), _hash(email), "subscribed"),
                )
        except Exception as e:
            # Unique violation or other
            flash("You're already on the list. 👍", "success")
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscribe_events(created_at, email_hash, status) VALUES(%s,%s,%s)",
                    (_now_iso(), _hash(email), "duplicate"),
                )
        return redirect(url_for("index"))
    # sqlite fallback
    try:
        db.execute(
            "INSERT INTO subscribers(email, created_at) VALUES(?, ?)",
            (email, _now_iso()),
        )
        db.commit()
        flash("You're subscribed! 🎉", "success")
        db.execute(
            "INSERT INTO subscribe_events(created_at, email_hash, status) VALUES(?,?,?)",
            (_now_iso(), _hash(email), "subscribed"),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("You're already on the list. 👍", "success")
        db.execute(
            "INSERT INTO subscribe_events(created_at, email_hash, status) VALUES(?,?,?)",
            (_now_iso(), _hash(email), "duplicate"),
        )
        db.commit()
    return redirect(url_for("index"))

@app.post('/events/job_view')
def events_job_view():
    data = request.get_json(silent=True) or {}
    log_job_view_event(
        data.get('job_id'),
        data.get('job_title'),
        data.get('company'),
        data.get('location'),
        normalize_country(data.get('location') or ''),
    )
    return {"ok": True}, 200

@app.get('/admin/metrics')
def admin_metrics():
    token = request.args.get('token')
    if token != os.getenv('ADMIN_TOKEN'):
        return ('forbidden', 403)
    db = get_db()
    top_titles = db.execute("SELECT norm_title, COUNT(*) c FROM search_events WHERE norm_title!='' GROUP BY 1 ORDER BY c DESC LIMIT 20").fetchall()
    top_countries = db.execute("SELECT norm_country, COUNT(*) c FROM search_events WHERE norm_country!='' GROUP BY 1 ORDER BY c DESC LIMIT 20").fetchall()
    recent = db.execute("SELECT created_at, norm_title, norm_country, result_count FROM search_events ORDER BY created_at DESC LIMIT 50").fetchall()
    return render_template('admin_metrics.html', top_titles=top_titles, top_countries=top_countries, recent=recent)

# ------------------------- Premium-ready API ----------------------------------
@app.get("/api/salary-insights")
def api_salary_insights():
    title_q = normalize_title((request.args.get("title") or "").strip())
    country_q = normalize_country((request.args.get("country") or "").strip())
    rows = enrich_with_salary_reference(read_jobs_csv())
    filtered = filter_jobs(rows, title_q, country_q)
    insights = []
    for r in filtered[:100]:
        ref_min = r.get("ref_salary_min")
        ref_max = r.get("ref_salary_max")
        smin = r.get("salary_min") or ref_min
        smax = r.get("salary_max") or ref_max or smin
        insights.append({
            "title": r.get("title"),
            "company": r.get("company"),
            "location": r.get("location"),
            "salary_min": smin,
            "salary_max": smax,
            "ref_median": r.get("ref_median"),
            "ref_currency": r.get("ref_currency"),
        })
    return jsonify({
        "count": len(filtered),
        "items": insights,
        "meta": {"title": title_q, "country": country_q}
    })

if __name__ == "__main__":
    # Background scheduler for refreshing salary reference cache
    try:
        scheduler = BackgroundScheduler(daemon=True)
        def refresh_salary_cache():
            try:
                # Reset cache so next call reloads
                _salary_cache.update({"path": None, "mtime": 0, "map": {}})
                # Proactively warm cache
                read_salary_reference()
            except Exception as e:
                logger.warning("salary cache refresh failed: %s", e)
        scheduler.add_job(refresh_salary_cache, "interval", minutes=int(os.getenv("SALARY_REFRESH_MIN", "30")))
        scheduler.start()
    except Exception as e:
        logger.warning("scheduler init failed: %s", e)
    app.run(debug=True)