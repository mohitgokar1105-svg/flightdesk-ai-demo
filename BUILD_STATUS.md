# BUILD_STATUS — FlightDesk AI

Legend: `[x]` done and verified by tests that were actually run · `[~]` partial · `[ ]` not done

_Last verification (2026-10-11): `python -m pytest` → **272 passed, 3 skipped** (the skipped tests are the opt-in paid
live tests); `ruff check .` clean; `mypy src app.py` clean (65 files). Browser check (headless Edge, separate temporary
database, AI off): customer request flow, team Inbox → inquiry workspace → AI actions, owner Overview — no exceptions.
The app on http://localhost:8501 was restarted with this version; the real team-member login lands on the Inbox.
The live assistant evaluation has **not** been run (needs cost approval)._

## Round 5 — multi-method evaluation (see EVALUATION_REPORT.md)
- [x] Live assistant evaluation run (full suite twice, regression suite, model comparison), live extraction
      evaluation, scale test, accessibility audit, security scan, dependency audit, secret scan, coverage.
- [x] Fixed from findings: like-for-like period comparisons; inquiry search date filtering beyond 50 rows and
      multi-word search; "restricted" marker instead of empty values; richer assistant tools (names in attention
      items, opportunity value and booking status in lists, trips by travel date, follow-up due status); prompt
      rules (assistant-v4); scorer bugs (typographic apostrophes, negated/conditional/quoted claims); date-window
      normalisation in extraction; N+1 queries (Overview 2.75× faster at 2,000 inquiries); text contrast.
- Remaining: AE-025, AE-075, AE-102, AE-079 (see report); no held-out test set yet; full v4 run not done.

## Round 4c — PDFs
- [x] Customer "charter options" PDF from recorded live quotes (`src/services/document_service.py`, fpdf2 + bundled
      DejaVu font): branded header, trip summary + distance, options by all-in price, inclusions/conditions/validity,
      "not a booking" disclaimer; operator names hidden by default; expired quotes left out and reported; audit event.
- [x] Cover note: standard / none / AI-written (`write_cover_note`; withheld if it adds numbers or claims a booking).
- [x] Assistant tool `prepare_quote_pdf` (download button in chat; never sent); prompt version assistant-v2.
- [x] Read an operator's quote PDF (pypdf text → AI structured reading → grounding check on prices → pre-filled form,
      never auto-saved; injection flagged; image-only/encrypted/oversized files refused).
- [x] Tests: `tests/integration/test_documents.py` (15). Browser check: PDF created and downloaded from the inquiry
      workspace; operator PDF uploaded and read (AI off → text shown).
- Not run: a live (paid) AI reading of a real operator PDF — covered by mocked tests only.

## Round 4b — airport suggestions and visuals
- [x] Type-ahead **airport/city suggestions** on every From/To field (5,333 airports, public-domain OurAirports data,
      bundled; fuzzy search by name/city/country/IATA/ICAO; free text still accepted). `src/places.py`,
      `scripts/build_places.py`.
- [x] **Route map + great-circle distance** (km / nm) on the customer form, its review step and every inquiry.
- [x] Owner Overview: **demand map** (routes requested in the period; unrecognised or ambiguous places are counted, never
      guessed) and **request → booking funnel** (one cohort, cumulative stages, from records).
- [x] Navy **hero banners** with a flight-path illustration (customer landing page, team Inbox).
- [x] Customer form error messages now clear as soon as the visitor edits a field.
- [x] Tests: `tests/unit/test_places.py` (search, labels, place resolution incl. ambiguity, distance, funnel).
- Limitation: map outlines load from Plotly's CDN (browser needs internet). Palm Beach International (PBI) is missing
  from the current OurAirports export, so "Palm Beach" isn't placed on maps.

## Round 4 audit (customer portal + simplification)
- **Worked (kept):** login/roles, inquiries, quotes, bookings, follow-ups, assistant + approvals, analytics, evaluation lab.
- **Missing:** any way for customers to submit a request; a team "what do I do next" view; the inquiry view mixed
  everything on one long page and AI actions were spread across pages.
- **Too complicated:** the owner Overview had a chart, quick actions and five cards competing; 9 flat menu items for
  every role; team members landed on an owner-style dashboard.
- **Security gaps found and fixed:** 22 read-only service functions (dashboard KPIs, needs-attention, activity, inquiry/
  follow-up/quote/customer lists, audit log, CSV frames) had no permission check — they only relied on page guards.
  All now call `require()`; a test proves the customer principal is refused by internal services.
- **Bug found and fixed:** `scripts/seed_demo_data.py` and `scripts/run_evaluation.py` failed with "Please sign in"
  since round 3 (they didn't run as the system principal). Regression test added.

## Implemented in round 4
- [x] **Customer portal** (`/request`, `/my-request`; `src/services/portal_service.py`, `src/ui/portal.py`): branded
      landing ("Where would you like to fly?"), 3-step form with progressive disclosure, friendly validation, review,
      reference + next steps, idempotent resubmission, graceful save failures, status check by reference + email
      (customer-safe fields only), per-session lookup limit, no account needed, independent-demo disclaimer.
- [x] **Customer role** (`Role.CUSTOMER`, `PORTAL` principal, `SUBMIT_INQUIRY` only); customers can't be created as
      user accounts; portal requests match existing customers by email only and never modify them (name mismatch is
      flagged for the team); instruction-like text in requests is flagged.
- [x] **Team Inbox** (`src/services/workspace_service.py`, `src/ui/inbox.py`): new (website first) / in progress /
      waiting on customer / follow-ups due (with Done) / recently done; open an item in place.
- [x] **Inquiry workspace** (shared by Inbox and Inquiries): next step, AI actions — Extract flight details
      (`reread_inquiry`: suggests values for empty fields only; team applies), Find missing information, Compare quotes
      (recorded values), Draft customer email (pre-selects the email type), Summarize inquiry (deterministic, optional AI
      rephrasing withheld if it adds numbers) — plus sections: details + internal notes, quotes, emails & follow-ups
      (Mark done), activity history (audit log), edit/status.
- [x] **Owner Overview** simplified: five numbers (new inquiries, quotes in progress, confirmed bookings, follow-ups
      needing attention, confirmed booking value), plain summary, Needs attention, recent activity, "Ask AI about your
      business" with the five example questions, collapsible trend, money-terms glossary (budget / estimated opportunity
      / quoted / confirmed booking value / revenue / profit).
- [x] **Navigation by role:** owner lands on Overview, team on Inbox; secondary items under "More"; anonymous visitors
      only get sign-in and the two customer pages. Login page links customers to the request form.
- [x] **Owner account management** in Settings (add team member, switch accounts on/off; can't switch off yourself).
- [x] **Tests:** `test_customer_workflow.py` (29 incl. parametrized cases) covering the 12 required workflows; `test_portal_ui.py` (5) through the
      real UI incl. `app.py` routing; `test_scripts.py` (1).
- [x] README updated (three experiences, URLs, permission matrix incl. customers, workflow tour, limitations).

## Round 3 audit (before this round)
- **Worked:**
  - extraction and review, the chat assistant with approval-gated tools, quotes, bookings, follow-ups;
  - owner analytics, the extraction evaluation (127 cases), CSV, migrations, and 166 tests.
- **Missing:**
  - authentication and roles: anyone with the URL had full access, including deleting data;
  - an evaluation of the *chatbot* itself (only extraction was evaluated);
  - written design research;
  - deterministic time for evaluations;
  - handling of failed database writes during approvals (a raw DB error would surface).
- **Weak:** the "AI quality lab" was buried under *Advanced*, and its name was technical.

## Implemented in round 3
- [x] **Auth** (`src/auth.py`, `users` table, schema v3):
  - Passwords: salted scrypt hashes; generic login errors; lockout after 5 failures for 5 minutes; 30-minute idle
    expiry; sign-out; change your own password.
  - Accounts: bootstrapped from `.env` only, never overwritten, no plaintext passwords in source or docs;
    `scripts/manage_users.py`; logins and denials audited.
- [x] **Roles:** business owner (all permissions) and team member (view, edit operations, use the assistant).
  - Enforced in the services and workflow engine with `require()`.
  - Navigation is filtered by role, and pages are guarded.
  - Financial values are blanked at the source.
  - Assistant tools are filtered and re-checked when called and again on approval.
- [x] Branded login page; navy and warm-white design system; DESIGN_RESEARCH.md.
- [x] Navigation: Overview · Inquiries · Quotes · Customers · Follow-ups · AI Assistant · AI Evaluation Lab · Reports ·
      Settings, plus Advanced (AI activity log).
- [x] Overview: reporting periods include last week and last month. Team members see overdue follow-ups instead of money.
- [x] Assistant:
  - the 8 required suggested questions;
  - date-range arguments;
  - follow-up counts per inquiry;
  - a "Data used for this answer" panel;
  - failed DB writes on approval are rolled back and reported, never claimed as done.
- [x] **AI Evaluation Lab, assistant suite** (see AI_EVALUATION.md):
  - Dataset: 114 cases, 20 categories, versioned (v1.0.0), from a generator script.
  - Fixture: fixed fictional data with verified expected facts; frozen clock.
  - Runner: uses the production `run_chat_turn` on a fresh DB per case, as the case's role; injected tool and API
    failures; approval and persistence verification.
  - Scoring: 12 deterministic checks.
  - Metrics: every one stored with numerator and denominator.
  - Cost: estimate, confirmation and cap.
  - Dashboard: run history, inspect, side-by-side, compare runs, regression list, CSV and JSON export; CLI
    `scripts/run_assistant_eval.py`.
- [x] Extraction evaluation: kept as the "Message reading" tab; F1 added for missing-info and validation flags.
- [x] Tests added:
  - auth: hashing, no plaintext, generic errors, lockout, inactive users, bootstrap, own-password change, the permission
    matrix, backend denials, hidden financials, tool filtering;
  - UI login and role gating;
  - the evaluation harness (17 tests);
  - failed DB writes.
- [x] Docs: README (sign-in, permission matrix, Evaluation Lab, commands, limits), AI_EVALUATION.md,
      DESIGN_RESEARCH.md, `.env.example` placeholders.

## Data files
- `data/flightdesk.db`: the app database, migrated to schema v3 on start.
- `data/*_backup.db`: earlier databases, kept and git-ignored. Safe to delete.

## Remaining / limitations
- **The live assistant evaluation has not been executed.**
  - It costs about $0.50 for the 23-case smoke suite and about $2.50 for all 114 cases (estimates).
  - Run it from the Lab or with `python scripts/run_assistant_eval.py --suite smoke`.
  - No assistant-quality numbers exist until then.
- Evaluation scoring is regex- and heuristic-based, so inspect failures by hand.
- No HTTPS, SSO, MFA or email password reset. Sessions live in server memory, so **refreshing the tab signs you out**.
  SQLite. Not hardened for internet exposure; the customer form shares a server with the staff sign-in.
- Customer portal: no customer accounts (reference + email instead); customers aren't emailed automatically; the
  status-check limit is per browser session only (no server-side rate limiting).
- No email sending, operator integrations, currency conversion or profit, by design.
- The API key was shared in chat during development. **Rotate it.** An old key is also set globally in the Windows
  shell. The project's `.env` overrides it.
- This is not a git repository, and nothing has been committed.
