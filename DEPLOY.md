# Deploying D2C Finance OS

Recommended setup: **Supabase** holds the database and **Render** runs the app. Render
builds the app from this GitHub repo and redeploys every time you push to `main`.

| Piece | Service | Cost (approx.) |
|---|---|---|
| Database (Postgres) | Supabase, region **Mumbai (ap-south-1)** | Free tier to start; Pro $25/month for daily backups |
| App (FastAPI + dashboard) | Render web service, Docker, region Singapore | Starter $7/month (Free works for trying, but sleeps when idle) |

Allow about 30 minutes the first time.

---

## Step 1 — Create the database (Supabase)

1. Go to https://supabase.com → **New project**.
   - Name: `d2c-finance-os`; Region: **South Asia (Mumbai)**.
   - Set a strong **database password** and save it in your password manager.
2. When the project is ready, open **SQL Editor → New query**. Paste the whole file
   `supabase/migrations/0001_init.sql` and click **Run**. This creates every table,
   row-level security and the locked-month protection.
3. Click **Connect** (top of the dashboard) → **Session pooler** and copy the URI. It looks like
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
   Replace `[YOUR-PASSWORD]` with your database password. This is your **DATABASE_URL**.
   Use the *Session pooler* string: the "Direct connection" string needs IPv6, which Render
   doesn't support.

## Step 2 — Deploy the app (Render)

1. Go to https://render.com and sign up **with GitHub**. Allow Render to access the
   `d2c-finance-ERP` repository.
2. **New → Blueprint** → choose `d2c-finance-ERP`. Render reads `render.yaml` from the repo.
3. It asks for `DATABASE_URL`: paste the string from step 1.3. Click **Apply**.
4. Wait for the build (about 3–5 minutes). Your app is then live at
   `https://d2c-finance-os-xxxx.onrender.com`.
5. Find your login: Render → your service → **Environment** → `APP_PASSWORD`, a strong
   value Render generated. The user name is `admin`.
6. Open the URL and sign in when the browser asks.

## Step 3 — Load data

- **Real brand:** *Import data* tab → **New brand** → upload CSVs in the order listed there.
  Download blank templates from the same tab.
- **Demo data, for showing prospects:** set `ENABLE_DEMO=true` in Render → Environment
  → Save (the service restarts), then click **Load demo brand**. Use a separate Render
  service for demos so demo brands never mix with client data.

## Step 4 — Your own domain (optional)

Render → service → **Settings → Custom Domains** → add `app.yourcompany.in`. Then create the
CNAME record Render shows at your domain registrar. HTTPS is set up automatically.

## Updating the app

Push to `main` on GitHub, and Render rebuilds and redeploys automatically (`autoDeploy: true`).
Watch the progress under **Events** on the Render dashboard. To roll back, open **Events**
and pick an earlier deploy.

---

## Other ways to run it

The repo has a standard `Dockerfile`, so any container host works: Railway, Fly.io, Google
Cloud Run, AWS (App Runner / ECS in **ap-south-1 Mumbai**, if client data must stay in India),
or your own server.

```bash
docker build -t d2c-finance-os .
docker run -p 8000:8000 -e DATABASE_URL="postgresql://..." -e APP_PASSWORD="choose-a-strong-one" d2c-finance-os
```

Without Docker (on a laptop or office machine):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_PASSWORD=choose-one .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes in production | Postgres connection string. Without it the app uses a local SQLite file, which is **wiped on every Render deploy**. |
| `APP_PASSWORD` | yes in production | Password for the whole app (HTTP Basic sign-in). If it's not set, the app is open to anyone. |
| `APP_USERNAME` | no | Sign-in name, default `admin`. |
| `ENABLE_DEMO` | no | `true` shows the *Load demo brand* button. Default: off when a password is set. |
| `PORT` | no | Set automatically by Render and other hosts. |

---

## Before real client data goes in — checklist

- [ ] `APP_PASSWORD` is set and shared only with your team.
- [ ] `DATABASE_URL` points to Supabase (not SQLite) and the migration has been run.
- [ ] Supabase **Pro** plan (daily backups), or a scheduled `pg_dump`.
- [ ] The repo on GitHub is **Private**.
- [ ] **Still to build:** per-user login (Supabase Auth) instead of one shared password, and
      exact money amounts (`NUMERIC`) instead of floats. Do both before onboarding paying clients.
- [ ] A data-processing agreement with each client covering India's DPDP Act, 2023.
      Note that Render's nearest region is Singapore; use AWS Mumbai if a client
      requires data to stay in India.
