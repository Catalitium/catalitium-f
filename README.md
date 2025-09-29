# Catalitium

Quick start
- Create a virtualenv and install requirements: `pip install -r requirements.txt`
- Optional: set `SUPABASE_URL` to use Postgres; otherwise SQLite is used.
- Run locally: `python app.py`

Environment variables
- SECRET_KEY: Flask secret
- SUPABASE_URL: Postgres connection string (e.g., from Supabase)
- DATA_ENC_KEY: Optional Fernet key for CSV encryption (use `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- JOBS_ENCRYPTED, SALARY_ENCRYPTED: `1` to enable decrypting CSVs at runtime
- SALARY_REFRESH_MIN: minutes between background refreshes (default 30)

Database schema
```sql
CREATE TABLE IF NOT EXISTS subscribers (
  email TEXT UNIQUE,
  created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS search_logs (
  term TEXT,
  country TEXT,
  created_at TIMESTAMP
);
```

Deploy
- Fly.io: see `fly.toml` and `Dockerfile` (optional)
- GitHub Actions: see `.github/workflows/deploy.yml`
# Catalitium — High-Signal Job Board

Catalitium is focused on **search-first UX** and **high-quality job alerts**.

---

## ✨ Features
- 🔎 **Smart job search** (title synonyms, fuzzy matching, country normalization)
- 📊 **Salary enrichment** from `salary.csv` (city → country → global fallback)
- 💰 **Delta badges** vs reference salaries
- 📧 **Weekly job reminders** — subscribe via modal (stored in SQLite)
- 📈 **Google Tag Manager (GTM)** events for search, views, subscriptions
- 🗂 **Simple stack**: Flask + SQLite + CSV files + Tailwind via CDN
- ⚡ **Pagination** (100 results per page)

---

## 📊 Data Model

SQLite schema (auto-created):

CREATE TABLE subscribers (
  email TEXT UNIQUE,
  created_at TEXT
);
CREATE TABLE search_logs (
  term TEXT,
  country TEXT,
  created_at TEXT
);


Jobs and salary data are kept flat in jobs.csv and salary.csv.

## 🛠 Requirements

Python 3.11+

Flask ≥ 2.2

gunicorn ≥ 21.2

python-dotenv ≥ 1.0

Install:

pip install -r requirements.txt

## 🧭 Roadmap

✅ MVP: CSV jobs + salary enrichment + subscribe

⏳ Next: JSON API endpoint

⏳ SEO + sitemap

⏳ Admin UI / CMS for posting jobs

## 🤝 License


MIT © 2025 Catalitium
