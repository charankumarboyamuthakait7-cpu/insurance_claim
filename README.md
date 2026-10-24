# ClaimCare — Health Insurance Claims Processing Simulator

A Flask + MySQL classroom simulator for four role-specific workflows: Policyholder, Hospital Desk, Claims Officer, and Administrator. All people, policies, claims, amounts, and outcomes are fictional. No actual insurance decision or payment is made.

## Important: use your existing database

This project is intended to connect to your existing MySQL database. It does **not** create, drop, reset, or migrate tables automatically. Before running it, configure `.env` with the database name and credentials you already use. Do not run `seed.py` against a database containing your real project data; the seed script is only for a disposable local demo database.

```text
SECRET_KEY=use-a-long-random-secret
DATABASE_URL=mysql+pymysql://USER:URL_ENCODED_PASSWORD@localhost:3306/EXISTING_DATABASE_NAME
DEMO_PASSWORD=your-local-demo-password
```

URL-encode reserved characters in the database password (for example `@` becomes `%40` and `#` becomes `%23`). Never commit `.env` or share its password.

## Setup on Windows

```powershell
# Activate your existing virtual environment first
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set your existing MySQL database name and credentials
python run.py
```

Open `http://127.0.0.1:5000`. Keep the existing database tables and records intact. If the ORM model fields do not match the database schema, stop and reconcile the mapping to the actual schema; do not use `db.create_all()`, migrations, or the seed script as a workaround.

## Main application areas

- `app.py` — app factory, security configuration, template helpers, role gates, error handling.
- `models.py` — SQLAlchemy models and workflow constants.
- `services.py` — business validation, claim workflow, eligibility calculations, history and settlement rules.
- `routes/` — role-specific route modules.
- `templates/` — shared shell and role-specific screens.
- `static/css/style.css` and `static/js/app.js` — responsive UI and small progressive enhancements.

## Workflow principles

- A claim is submitted only after server-side validation.
- Claims officers review submitted claims and must provide reasons for decisions.
- Requests for more information return to the original submitter for a response.
- Approval is not payment. An administrator records a **simulated** settlement only for an approved claim.
- Claim history is retained for auditability; access checks happen server-side.
- The document checklist tracks metadata only; it does not store actual hospital documents.

## Demo accounts

If (and only if) you intentionally create a separate disposable demo database and run the seed script for it, consult the seed script/seed documentation for the generated demo accounts. Do not run seeds against your existing populated database.
