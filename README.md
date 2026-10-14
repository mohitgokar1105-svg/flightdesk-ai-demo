# FlightDesk AI

**An AI-assisted operations desk for a private-aviation (charter) business, with three connected experiences on one
database:**

| Who | Where | What they do |
|---|---|---|
| **Customers** (no account) | `http://localhost:8501/request` | "Where would you like to fly?" — a 3-step flight request form, a reference number, and a status check at `/my-request` |
| **Team members** | sign in at `http://localhost:8501` → **Inbox** | Work queue: new requests, in progress, waiting on customer, follow-ups due — open an item and process it on the same page with built-in AI actions |
| **Business owner** | sign in → **Overview** | A few key numbers, a plain-English summary, **Ask AI about your business**, plus everything the team has; reports, AI Evaluation Lab, team accounts and settings under **More** |

**The workflow:** a customer submits a request → it appears at the top of the team's Inbox (tagged *From website*) →
the team checks the details (✨ *Extract flight details*, 🔎 *Find missing information*), records and compares operator
quotes, drafts and reviews emails, and updates the status → the owner's Overview and Reports update from the same
records.

> **Independent portfolio prototype** (project codename "Flygreen AI FlightDesk"). It is **not** an official Flygreen
> product and is not connected to Flygreen's or any operator's systems. All customers, quotes and bookings in the demo
> are fictional. FlightDesk never sends email, never contacts operators, and never makes real bookings or payments.

---

## 1. Quick start

```bash
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                 # macOS/Linux: cp .env.example .env — then edit it (see below)
streamlit run app.py                   # team/owner: http://localhost:8501 · customers: http://localhost:8501/request
```

### Signing in

FlightDesk requires a login. **No usernames or passwords are written in the source code or in this README.** On startup
the app creates the two demo accounts *only if* their details are present in `.env` (and never overwrites an existing
account):

```
FLIGHTDESK_OWNER_USERNAME=<choose>     # role: business owner (full access)
FLIGHTDESK_OWNER_PASSWORD=<choose, 8+ characters>
FLIGHTDESK_USER_USERNAME=<choose>      # role: team member (day-to-day work only)
FLIGHTDESK_USER_PASSWORD=<choose, 8+ characters>
```

Alternatively create users from the command line (password is prompted, never echoed):
`python scripts/manage_users.py create --username <name> --role business_owner` · `... list` · `... deactivate --username <name>`.

Passwords are stored only as salted **scrypt** hashes. Login errors are generic (they don't reveal whether a username
exists); 5 wrong passwords lock the account for 5 minutes; sessions expire after 30 minutes of inactivity; **Sign out**
is in the sidebar; a signed-in user can change their own password in Settings. Logins and permission denials are written
to the audit log. ⚠ The demo accounts are for local demonstration only — **they are not production credentials**; use
strong, unique passwords for anything else.

### Who can do what

| Permission | Business owner | Team member | Customer (website) |
|---|:-:|:-:|:-:|
| Submit a flight request; check *its own* status with reference + email | ✓ | ✓ | ✓ |
| Inbox; see inquiries, quotes (incl. operator prices), customers, follow-ups | ✓ | ✓ | — |
| Create/edit inquiries, quotes, follow-ups, internal notes; write emails | ✓ | ✓ | — |
| Use the AI assistant (read tools + approval-gated drafts/to-dos) | ✓ | ✓ | — |
| Overview, Reports, money totals (booked / pipeline / quoted value), financial assistant tools | ✓ | — | — |
| Record, complete or cancel bookings | ✓ | — | — |
| CSV import/export; load sample data / start over | ✓ | — | — |
| AI Evaluation Lab; AI activity log | ✓ | — | — |
| AI/system settings; add team members; switch accounts on/off | ✓ | — | — |

The website runs as a fixed **customer** principal that holds only the "submit request" permission — it never signs in,
can't open any internal page (they aren't even registered for anonymous visitors), and its two operations return only a
reference or a customer-safe status (no prices, notes, operators or other customers). A status check needs the reference
**and** the email used; a wrong combination looks exactly like a missing reference. There is no role switcher: roles come
from the user record and can only be changed by the owner.

Permissions are enforced **in the backend services** (`require(Permission…)` in `src/auth.py`, called by the service
and workflow functions), not only by hiding buttons: a team member calling a financial or admin function gets
`PermissionDenied`, the assistant is only offered tools the signed-in role may use, and every tool call and approval is
re-checked. Money values are blanked at the source for roles without the financial permission.

On first launch the owner's Overview offers **Add your first inquiry** or **Explore with sample data**.
(Or load the fictional sample data from the command line: `python scripts/seed_demo_data.py`.)

### Switching the AI on

1. Create an API key at <https://platform.openai.com/api-keys>.
2. Copy `.env.example` to `.env` (it is git-ignored) and set:
   ```
   OPENAI_API_KEY=sk-...
   DEMO_MODE=false
   OPENAI_MODEL=gpt-4.1
   ```
3. Restart. **Settings → AI assistant → Test the connection** confirms it works.

The project's `.env` takes priority over a key set globally in your shell (so an old key elsewhere can't break the app).
The key is never displayed, logged, or stored in the database. Without a key, everything except AI writing still works:
messages are read by a labelled basic reader, emails come from templates, and the assistant answers its suggested
questions straight from your records — it never pretends to be AI.

---

## 2. What you can do

| Page | What it's for |
|---|---|
| **Customer request form** (`/request`) | Landing page → *Request a flight* → 1. trip (one way / round trip, from, to, dates, passengers) → 2. contact (name, email, optional phone) → 3. optional extras (time, aircraft, special requests) and review → reference + "what happens next". Friendly validation (past dates, return before departure, same origin/destination, 1–50 passengers, email format); resubmitting the same request returns the same reference; a save failure keeps the form filled in. Nothing claims a quote or booking. |
| **Inbox** (team home) | Counts and sections: *New* (website requests first), *In progress*, *Waiting on customer*, *Follow-ups due* (today/overdue, with **Done**), *Recently done*. **Open** shows the inquiry right there: next step, AI actions (✨ Extract flight details · 🔎 Find missing information · ⚖️ Compare quotes · ✉️ Draft customer email · 📝 Summarize inquiry), and sections for details + internal notes, quotes, emails & follow-ups (**Mark done**), activity history, and edit/status. |
| **Overview** (owner home) | Five numbers — new inquiries, quotes in progress, confirmed bookings, follow-ups needing attention, confirmed booking value — a plain-English summary, **Needs attention**, recent activity, **✨ Ask AI about your business** with example questions, a collapsible 12-week trend, and *What do the money terms mean?* (budget vs estimated opportunity vs quoted vs confirmed booking value vs revenue vs profit). |
| **Inquiries** | Paste a customer's email → FlightDesk reads the name, contact details, route, dates, time preference, passengers and special requirements. Each item is marked **✓ from the message**, **⚠ please check** (interpreted) or **— missing**. You correct anything and click **Save**. Then: suggested next steps, "Ask AI to review", status, edit. |
| **Quotes** | Enter operator quotes (price + fees, seats, inclusions, conditions), compare them side by side on the all-in price, get questions to ask the operator, optionally an AI plain-English explanation, and record a confirmed booking (with your OK). Bookings tab: complete or cancel trips. |
| **Customers** | Contact details, every trip, and what each customer has booked (per currency). |
| **Follow-ups** | Write an email — initial response, request missing details, quote follow-up, booking confirmation (draft), general update, and more — with AI or a template. Edit, copy, or **Open in my email app**. To-do list with due dates, snooze, mark as sent/done. |
| **Ask AI** | Chat: *How is the business doing this month? Which inquiries need follow-up? What should I prioritize today? Draft a reply to …* Answers come from your records via a small set of approved tools (each answer has a **Data used for this answer** panel); anything that would change a record appears as a card with **Approve / Discard**. Also available from the sidebar (**✨ Ask the assistant**). |
| **AI Evaluation Lab** | Owner only. Runs a 114-case test set against the *real* assistant (fixed fictional data, frozen date) and scores it deterministically: pass rate, per-category results, tool choice/arguments, facts, unsupported claims, refusals, security, persistence, latency, tokens, estimated cost. Inspect cases side by side, compare runs, export. Also the 127-case message-reading evaluation. See [AI_EVALUATION.md](AI_EVALUATION.md). |
| **Reports** | Detailed numbers by period/status/service with exactly how each is counted (numerator, denominator), pipeline vs quoted vs booked value per currency, funnel, record drill-downs and CSV exports. |
| **Settings** | Your account (change password), and for the owner: team accounts (add, switch on/off), AI status and connection test, sample data, start over, CSV import, technical details. |
| *More* | Owner: **Reports**, **AI Evaluation Lab**, **AI activity log** (every AI run, tool call and approval), the customer request form (to preview it), **Settings**. Team: the customer request form and Settings. |

---

### Airport suggestions, route maps and charts

* **From / To fields** (customer form, inquiry review and edit) suggest airports as you type — search by airport name,
  city, country or IATA/ICAO code (e.g. "luton", "Mykonos", "TEB", "KTEB"). Anything not listed can still be typed and
  is saved as written. The list (5,333 airports: large and medium airports with an IATA code, plus small ones with
  scheduled service) is bundled in `src/reference/airports.csv`, built from the public-domain
  [OurAirports](https://ourairports.com/data/) data by `python scripts/build_places.py`. It works offline, costs
  nothing, needs no API key, and never sends what customers type to a third party.
* **Route map + distance** on the customer form (as soon as both places are chosen), on its review step, and on every
  inquiry: the great-circle route and distance in km / nautical miles, computed from airport coordinates — clearly
  labelled as a straight-line distance, not a flight time.
* **Owner Overview:** *Where customers want to fly* (routes requested in the period, thicker = more requests; places
  that can't be recognised are counted, not guessed — e.g. an ambiguous "Portland") and *From request to booking*
  (one cohort: inquiries received → quotes recorded → quote sent → booked, each stage from the records).
* **Team Inbox:** a banner with today's workload. Maps load their country outlines from Plotly's CDN, so they need an
  internet connection in the browser; everything else works offline.

### Look and feel

The sign-in screen is a dark animated scene: a rotating wireframe globe with planes flying along routes, a radar sweep,
drifting stars and a glass sign-in panel. The ticker shows only real facts: airports indexed, AI model, latest
evaluation score and security rules. After sign-in, a roughly 3-second launch sequence shows your real role, permission
count and record counts, then fades into the app. Inside the app there's a faint blueprint grid, glowing cards and
animated banners.

All of it is pure CSS and SVG with no scripts. It switches off automatically if your system asks to reduce motion, and
**Settings → Futuristic effects** turns it off for your session. Change the "home base" airport on the sign-in screen
with `FLIGHTDESK_HOME_AIRPORT` (default `CLT`).

### PDFs

* **Customer PDF ("Your charter options")** — on every inquiry's *Quotes* section and on the Quotes page: one click
  turns the recorded live quotes into a branded A4 PDF (trip summary, straight-line distance, options sorted by all-in
  price, inclusions, conditions, validity, and a clear "not a booking, prices not guaranteed" note). Operator names are
  hidden by default (Option A/B/C). Quotes past their validity date are left out (and listed for you). Cover note:
  standard, none, or **AI-written** — the AI note is withheld and the standard one used if it mentions any number not
  in your records or claims anything is booked/guaranteed. FlightDesk never sends the PDF; you attach it yourself.
* **Ask AI**: "Make a PDF of Clara's options" → the assistant prepares it and a **Download PDF** button appears under the
  answer.
* **Read an operator's quote PDF** (Quotes page): upload the operator's PDF → the AI reads it and pre-fills the quote form
  (operator, aircraft, seats, price, fees, currency, inclusions, conditions, validity). Prices the AI reports that don't
  appear in the document are left blank with a warning; instructions hidden in the document are flagged and ignored;
  nothing is saved until you check the form and click **Save quote**. Without AI, the PDF's text is shown to copy from.
  Scanned (image-only), password-protected or >10 MB PDFs are refused with a plain explanation.
* Libraries: `fpdf2` (writing) and `pypdf` (reading); the bundled DejaVu font (free licence, `src/reference/fonts/`)
  prints accented names correctly.

---

## 3. How the AI is kept honest (design)

* **The model never touches the database.** It can only call 14 allowlisted tools (filtered by the signed-in role) with strict, validated arguments
  (IDs must match patterns; enums; bounded lengths). There is no SQL, file, shell or code tool.
* **Numbers come from Python.** KPIs, conversion rates, quote comparisons and summaries are computed by deterministic
  functions (`src/services/dashboard_service.py`, `analytics_service.py`, `quote_service.py`); the model only explains
  them. The AI dashboard summary and quote explanations are **withheld** if they contain any figure not present in the
  computed facts.
* **Writes need you.** `draft_email`, `mark_follow_up_complete`, status changes and suggestion decisions create
  *proposals*; nothing changes until you click Approve, and arguments are re-validated at that moment. The assistant
  cannot confirm bookings, record quotes, change prices, delete or send anything — those tools are not offered to it.
  It only says something is done after the database operation succeeded.
* **Customer text is untrusted.** Messages are wrapped as data in prompts, labelled `customer_message_untrusted` in tool
  results, checked by injection heuristics (and the model's own flag), and can never trigger actions on their own.
* **Extraction is always reviewed.** Model output is validated with Pydantic, normalised, checked against the source
  text (grounding), and marked stated / inferred / missing; ambiguous places ("Portland") and dates ("03/04") are left
  for you to confirm rather than guessed. Inquiries are saved only from the review form.
* **Bounded and observable.** Max 6 model turns and 8 tool calls per request, duplicate calls rejected, bounded retries
  only for timeouts/rate limits/5xx (never for invalid output), every run recorded in the AI activity log.

### Financial rules in the reports
Inquiries are never bookings; quotes are never revenue; *pipeline (your estimates)*, *quoted* (cheapest live all-in
quote per inquiry, so alternatives aren't double-counted) and *booked* (confirmed + completed, cancelled excluded) are
kept separate; currencies are never added together; booking conversion = inquiries received in the period with a
confirmed/completed booking ÷ inquiries received in the period (each inquiry once). **Profit is not shown** — operator
costs and commissions are not recorded, so booked value is not profit.

---

## 4. Architecture

```
app.py                      Streamlit entry point: public pages (sign-in, /request, /my-request) or role-based nav
src/
  config.py                 settings from env/.env (key never displayed or logged)
  models.py / database.py   SQLAlchemy models (SQLite), schema versioning + migrations (v2: customers, quote sent
                            date & fees, new email types), guarded reset
  domain.py / schemas.py    statuses + transition tables; Pydantic validation (inquiries, quotes, customers, AI output)
  ai/                       OpenAI Responses API client (structured outputs, tool calling, bounded retries), prompts,
                            extraction (AI + rule-based reader), validation & grounding, drafting, assistant support,
                            evaluation framework
  services/                 inquiries, customers, quotes, bookings, follow-ups, recommendations, analytics,
                            dashboard (owner KPIs, needs-attention, activity, summary facts), CSV, audit,
                            portal (customer requests + status), workspace (team work queue, activity, summaries)
  workflows/                state machine, run recorder, tool registry + tools, workflow engine (intake, triage,
                            chat turn, drafting, booking approval, quote explanation, re-read, summarize)
  ui/                       one module per page + shared design system
  auth.py / clock.py        users, roles, permissions, password hashing, sessions; overridable clock (evaluations)
evals/datasets/             assistant_eval_v1.jsonl (114 cases), inquiry_extraction_v1.jsonl (127 cases), regressions list
scripts/                    seed_demo_data.py, manage_users.py, run_assistant_eval.py, build_assistant_eval_dataset.py,
                            run_evaluation.py, build_eval_dataset.py
tests/                      unit, integration (incl. Streamlit UI tests), evaluation, opt-in live API tests
```

Default model: `gpt-4.1` (verified available on the configured account; supports structured outputs, function calling
and `temperature`). Change it with `OPENAI_MODEL`.

---

## 5. Testing

```bash
python -m pytest                       # 277 tests (+3 opt-in live); no network, no API cost (the suite cannot read .env)
ruff check .  &&  mypy src app.py      # lint + types
# Opt-in live tests (3 small paid calls: extraction, tool-calling triage, assistant chat):
RUN_LIVE_TESTS=1 python -m pytest tests/integration/test_live_smoke.py -m live
# Live assistant evaluation (paid; asks first): 23 smoke cases ≈ $0.50, full 114 ≈ $2.50 (estimates)
python scripts/run_assistant_eval.py --suite smoke
```

Covered: dashboard metrics, date boundaries, periods, conversion, currencies, duplicate quotes, cancelled bookings, empty
database, persistence and migrations, inquiry/customer validation, AI output validation (real SDK through an HTTP mock),
missing key, timeouts/rate limits/retry bounds, tool argument validation, unauthorized writes, approval requirements,
email drafts and claims checks, prompt injection, offline answers, password hashing, login/lockout, role enforcement in
services and assistant tools, hidden financials, failed database writes, the evaluation harness (scoring, metrics, cost
cap, fault injection, comparison), and every page rendered via Streamlit AppTest for both roles. The customer → team →
owner workflow has its own tests (`tests/integration/test_customer_workflow.py`, `test_portal_ui.py`): valid and invalid
requests, duplicates, inbox, team updates, owner metrics, customer data isolation, team-vs-owner limits, AI tools vs
permissions, prompt injection from the website form, AI/API and database failures, recorded-value quote comparisons,
empty data and date boundaries, and role-based routing through the real `app.py`.

---

## 6. Try the whole workflow (5 minutes)

1. **As a customer:** open `http://localhost:8501/request` in a private/incognito window → **Request a flight** → fill in
   the three short steps → **Send request** → note the reference. (*Check status* shows "Received".)
2. **As a team member:** sign in at `http://localhost:8501` with the team account from `.env` → the **Inbox** shows the
   request under *New*, tagged *From website* → **Open the next new inquiry** → try 🔎 *Find missing information* and
   📝 *Summarize inquiry* → add an internal note.
3. Still in the inquiry: **⚖️ Compare quotes → Add or manage quotes** → enter two operator quotes → mark the better one
   *sent to customer* (the inquiry moves to *Waiting on customer*).
4. **✉️ Draft customer email** → **Write email** → edit → **Open in my email app** or **Save draft** (nothing is sent
   automatically). Mark follow-ups **Done** from the Inbox once handled.
5. **As the owner:** sign out, sign in with the owner account → **Overview** shows the new inquiry, quotes in progress
   and, once you record the booking on **Quotes** (owner only), the confirmed booking value in its own currency.
6. **✨ Ask AI about your business** → "Which quotes are awaiting a customer response?" → answers come from the records.
7. *More → AI activity log* → see every AI call, tool call, rejection and approval.

---

## 7. Known limitations

* Local prototype on SQLite: login and roles exist, but there is no HTTPS, SSO/MFA, password reset by email, or
  per-record ownership; sessions live in the Streamlit server's memory, so **refreshing the browser tab signs you out**.
  The customer form and the staff sign-in are served by the same app. A hardened single-host deployment (Docker, Caddy
  HTTPS, security headers, server-side rate limits, daily backups, CI) is provided — see [DEPLOYMENT.md](DEPLOYMENT.md).
* Customer portal: no customer accounts (by design — reference + email instead); customers are **not emailed**
  automatically (the team replies from their own email); status checks and requests are rate limited server-side per client (in-process).
* No email sending, calendar, CRM, operator or live aircraft-availability integrations — by design; drafts are copied or
  opened in your own email app.
* No currency conversion (values are shown per currency); no profit (costs aren't recorded).
* The basic (non-AI) reader understands clear English best; it misses typos, other languages and unusual phrasing.
* Deterministic safety checks are conservative and can flag consistent details (e.g. "3 of us + 2 colleagues = 5") as
  conflicting — they err towards asking.
* The AI summary/explanations are withheld (not "corrected") when they contain unsupported figures, so occasionally you'll
  see the plain summary instead.
* Chat history lives in the browser session; it is not saved between sessions (actions and AI runs are, in the audit log).
* Cost figures in the AI activity log are estimates from configurable assumed prices.
* Assistant evaluation scoring uses regular expressions and keyword heuristics — it can misjudge unusual phrasing;
  inspect failures before trusting a number.
* Single-instance only (SQLite, in-memory sessions); no MFA/SSO. Suitable for a small, supervised deployment — see DEPLOYMENT.md.
