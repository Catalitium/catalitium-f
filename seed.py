# seed.py (add this simple importer)
import os, sqlite3, csv, re
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "catalitium.db")
CSV_PATH = os.getenv("JOBS_CSV", "jobs.csv")

COUNTRY_FIX = {
    "deutschland":"DE","germany":"DE","deu":"DE","de":"DE",
    "switzerland":"CH","schweiz":"CH","suisse":"CH","svizzera":"CH","ch":"CH",
    "austria":"AT","österreich":"AT","at":"AT",
    "europe":"EU","eu":"EU",
    "uk":"UK","gb":"UK","england":"UK","united kingdom":"UK",
    "usa":"US","united states":"US","america":"US","us":"US",
}

def _now():
    return datetime.utcnow().strftime("%Y-%m-%d")

def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS subscribers (email TEXT UNIQUE, created_at TEXT);
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT NOT NULL,
        description TEXT NOT NULL,
        salary_min INTEGER,
        salary_max INTEGER,
        date_posted TEXT,
        featured INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS search_logs (term TEXT, country TEXT, created_at TEXT);
    """)
    conn.commit()

def parse_salary_range(s):
    if not s: return (None, None)
    nums = [int("".join(filter(str.isdigit, n))) for n in re.findall(r'\d[\d.,]*', str(s))]
    if not nums: return (None, None)
    if len(nums)==1: return (nums[0], None)
    return (min(nums), max(nums))

def normalize_country(val):
    if not val: return ""
    t = str(val).strip().lower()
    return COUNTRY_FIX.get(t, t.upper() if len(t)==2 else val)

def choose_location(row):
    city = (row.get("City") or "").strip()
    country = normalize_country(row.get("Country") or "")
    if city and country:
        return f"{city}, {country}"
    loc = (row.get("Location") or "").strip()
    if country and country not in loc:
        return (loc + (", " if loc else "") + country).strip(", ")
    return loc or country or "Remote"

def import_csv(conn, path):
    with open(path, newline='', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for r in rows:
        title = (r.get("JobTitle") or "").strip()
        company = (r.get("CompanyName") or "").strip()
        location = choose_location(r)
        desc = (r.get("NormalizedJob") or "").strip() or title
        smin, smax = parse_salary_range(r.get("Salary"))
        created = (r.get("CreatedAt") or "").strip()
        date_posted = created[:10] if created else _now()
        if not title or not company or not location:
            continue  # skip broken rows
        conn.execute("""
            INSERT INTO jobs (title, company, location, description, salary_min, salary_max, date_posted, featured)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (title, company, location, desc, smin, smax, date_posted))
    conn.commit()
    print(f"Imported {len(rows)} rows from {path}")

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    if os.path.exists(CSV_PATH):
        import_csv(conn, CSV_PATH)
    else:
        print(f"CSV not found at {CSV_PATH}")
    conn.close()
