# Deploying FlightDesk AI

One Docker image, run on a single host with SQLite on a persistent volume, behind Caddy for automatic HTTPS.

## 1. What you need
- A Linux VPS (1 vCPU / 1–2 GB RAM is enough) with Docker + Docker Compose.
- A domain name with an `A` record pointing at the VPS (Caddy fetches the TLS certificate automatically).
- Ports 80 and 443 open.

## 2. Configure
```bash
git clone <your-repo-url> flightdesk && cd flightdesk
cp .env.example .env
```
Edit `.env`:
```
SITE_ADDRESS=desk.example.com
OPENAI_API_KEY=sk-...            # a NEW key, never one that was shared or committed
DEMO_MODE=false
FLIGHTDESK_OWNER_USERNAME=<your username>
FLIGHTDESK_OWNER_PASSWORD=<long unique password>
```
The owner account is created on first start only if it does not already exist. Add team members afterwards in
**Settings**, then remove `FLIGHTDESK_OWNER_PASSWORD` / `FLIGHTDESK_USER_*` from `.env`.

## 3. Run
```bash
docker compose up -d --build
docker compose ps            # app should become "healthy"
docker compose logs -f app   # structured logs go to stdout
```
Open `https://desk.example.com` (staff sign-in) and `https://desk.example.com/request` (customer form).

## 4. What is already hardened
| Concern | How it is handled |
|---|---|
| TLS | Caddy, automatic Let's Encrypt, HSTS |
| Headers | `nosniff`, `X-Frame-Options: DENY`, referrer/permissions policy (`deploy/Caddyfile`) |
| Internal port | App is `expose`d to Caddy only, not published on the host |
| Container | Non-root user, runtime dependencies only, health check on `/_stcore/health` |
| Abuse of public pages | Server-side rate limits (status checks 10/15 min, flight requests 5/hour, per client) — `src/ratelimit.py` |
| Login brute force | Per-account lockout (5 failures → 5 min), audited |
| Secrets | Only via environment / `.env` (git-ignored, excluded from the image) |
| Backups | `backup` service writes a verified SQLite backup daily to `data/backups`, keeps 14 |
| CI | Lint, types, 277 tests, dependency audit and a container health check on every push (`.github/workflows/ci.yml`) |

## 5. Operations
```bash
# Update
git pull && docker compose up -d --build

# Manual backup / list / restore
docker compose exec app python scripts/backup_db.py
docker compose exec app ls data/backups
docker compose stop app
docker compose run --rm --no-deps -v flightdesk_flightdesk-data:/app/data app \
    cp /app/data/backups/<file>.db /app/data/flightdesk.db
docker compose start app

# Create or deactivate users
docker compose exec app python scripts/manage_users.py list
```
Copy `data/backups` off the host regularly (the volume is on the same disk as the database).

## 6. Known limits (be honest with yourself before going live)
- **One app process.** SQLite plus in-memory sessions and rate limits mean you should not run several replicas.
- **Refreshing the browser tab signs staff out** (Streamlit session state lives in server memory).
- No MFA / SSO / emailed password reset. Use strong unique passwords.
- Customer details sent to OpenAI when AI is on: add a privacy notice and decide on a retention policy before taking real
  customer data. The shipped data and branding are a fictional portfolio demo.
- The Docker image and compose stack are built by CI but were not run on the author's Windows machine (no Docker
  installed there); do a first deploy on a staging host.
- PostgreSQL is **not** supported yet: the migrations and some indexes are SQLite-specific.
