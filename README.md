# Gift Distribution Tracker

Flask app to track gift distribution per member. Admin uploads the Excel sheet; users (per branch)
search by Member Code / Name and update delivery details.

## Features
- Login; admin creates users per branch (Laxmi Nagar, Burari, Dwarka, Other). Users only see their branch's rows.
- Excel/CSV upload (admin: any branch; branch users: only rows of their own branch) with flexible header matching; re-upload updates rows and keeps saved delivery data.
- Search by Member Code or Name, all columns shown, status filter, pagination.
- `Amount Due = Policy Amt × Due`; auto remark ("Completely paid" / "Amount pending: Rs. X" / "Policy amount missing"), editable.
- Quick "Delivered" button, bulk mark delivered/pending, double-delivery warning.
- Upload preview before saving; Excel export / full backup; dashboard; history log; delete data (admin).
- Change own password; admin can disable users; account locks 15 min after 5 wrong passwords.
- Tracks who/when: delivered-to, receiver mobile, KYC, delivered date-time, last updated by.

## Run locally
```bash
python -m venv venv
venv\Scripts\activate        # Windows  (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
set ADMIN_PASSWORD=YourStrongPass   # Linux/Mac: export ADMIN_PASSWORD=...
python wsgi.py
```
Open http://127.0.0.1:5000 and log in as `admin` (default password `admin123` if not set).

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render → **New → Blueprint** → select the repo (uses `render.yaml`; creates the web service + free PostgreSQL).
3. When asked, set `ADMIN_PASSWORD`. Deploy.

Data lives in PostgreSQL via `DATABASE_URL`, so it survives redeploys. (Without it, SQLite is used locally only.)

## Structure
```
wsgi.py            entry point (gunicorn wsgi:app)
tracker/           app package: models, parser, routes
templates/ static/ UI
render.yaml        Render blueprint
```