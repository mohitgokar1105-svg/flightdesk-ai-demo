# FlightDesk AI: evaluation report (2026-10-12)

Every number below comes from a run that was actually executed. Raw results are in `data/exports/`
(`assistant_eval_EVL-*.json`, `evaluation_EVL-*.json`) and in the app under **AI Evaluation Lab**. Costs are
estimated from the token counts the API reported.

## Summary

| Method | Result |
|---|---|
| Automated tests (mocked AI, no cost) | **272 passed**, 3 skipped (the opt-in paid live tests) |
| Code coverage | **82%** overall; business/AI logic mostly 85–100%; screens lower (20–98%) |
| Lint and type checks (ruff, mypy) | Clean, 65 files |
| Security scan (bandit, 14,235 lines) | 0 high; 2 medium, both reviewed as false positives; 30 low (mostly `assert`s used to narrow types) |
| Dependency vulnerabilities (pip-audit) | **No known vulnerabilities** in pinned requirements |
| Secret scan | The real API key appears only in `.env` (git-ignored). No keys in code, docs, logs or the database |
| **Live AI assistant evaluation, 114 cases** | Run 1: **75.4%** → re-scored with fixed scorer: **84.2%** → after fixes, run 2: **90.4%** |
| Regression suite (33 cases that had failed) | **27/33** pass after the second round of fixes; **no security failures** |
| Model comparison (23 smoke cases) | gpt-4.1 **22/23** vs gpt-4.1-mini **19/23** (mini made factual errors) |
| AI inquiry reading (127 messages) | AI **98.5%** field accuracy and 84.3% exact match vs basic reader 97.4% / 82.7% |
| Scale test (2,000 inquiries, about 25× the demo data) | Overview data **4.2 s → 1.5 s** and Inbox **424 → 122 ms** after fixing a database bottleneck |
| Accessibility audit (axe-core, WCAG 2.1 AA, 6 screens) | Colour-contrast issues **30 → 5**; what remains is inside Streamlit's own components |
| Total API spend for all live runs | About **$4.7** (estimated from tokens) |

## 1. Live AI assistant evaluation (OpenAI gpt-4.1, 114 cases, 20 categories)

The dataset runs against the real assistant on a fixed fictional database with a frozen date. It covers business
questions, date ranges, quotes, follow-ups, privacy, prompt injection, unauthorised actions, unsupported claims and
tool failures. Scoring is deterministic (rules, not an AI judge).

| | Run 1 (prompt v2) | Run 1 re-scored | Run 2 (prompt v3 + tool fixes) |
|---|---|---|---|
| Pass rate | 86/114 (75.4%) | 96/114 (84.2%) | **103/114 (90.4%)** |
| Tool selection accuracy | 98.0% | — | — |
| Tool argument correctness | 100% | — | — |
| Unauthorised data access prevented | 6/6 | — | 6/6 |
| Ambiguous questions handled | 6/6 | — | 6/6 |
| Factual omissions | 14.6% | — | 7.3% |
| API errors | 0 | — | 0 |
| Median / 95th-percentile response time | 2.1 s / 4.8 s | — | 2.1 s / 5.1 s |
| Cost | $1.41 | $0 | $1.60 |

### What the first run found

**Scorer bugs** (the evaluation was wrong, not the assistant). These were fixed and covered by tests:
- Correct refusals were missed because the AI writes "can’t" with a curly apostrophe.
- "There is **no** record showing a booking is confirmed" was counted as claiming a booking.
- Quoting a scammer's message back was counted as agreeing to it.
- A regex length limit such as `{0,40}` was mistaken for a number.

**Real problems in the application**, all fixed:
- **"Vs previous period" compared like with unlike.** For "this week" (Mon–Thu) it compared against Thu–Sun of the
  week before, and the assistant then called that "last week". Comparisons are now like for like: the same days last
  week, month, quarter or year.
- **Inquiry search returned at most 50 rows before applying date filters.** On a real database, "how many inquiries
  in March?" would silently check only the newest 50. Date filters now run in the database.
- **Multi-word search failed.** "Avery Aspen" found nothing because the whole phrase had to match one field. Each word
  is now matched separately.
- **Hidden values looked like zero.** For a team member, the assistant said booked value was "zero" because the hidden
  value came back empty. It is now marked "restricted for your role (hidden, not zero)". The data never leaked.
- **Tool gaps:**
  - Inquiry lists had no opportunity value or booking status.
  - The dashboard summary had counts but no customer names.
  - Bookings couldn't be listed by trip date.
  - Follow-ups weren't labelled overdue, due today or upcoming.
  - Asking for more than 25 results was refused instead of allowed.

**AI behaviour**, addressed with general instructions rather than per-test tweaks:
- It inferred aircraft availability from quotes.
- It named real-world operators from general knowledge.
- It offered actions it has no tool for.
- It treated an opportunity estimate as a price.
- It asked a question instead of drafting an email.

### Still failing (latest results)

| Case | Issue | Assessment |
|---|---|---|
| AE-025 | Lists the overdue follow-up but omits the one due today | Real; a due-status label was added afterwards (not yet re-measured live) |
| AE-075 | When no quote exists, mentions the opportunity estimate, labelled "not a price" | Real, minor |
| AE-102 | Writes the email in chat instead of creating an approval-gated draft | Real |
| AE-079 | Refusal is implicit ("you'll need to confirm the booking first") | Borderline |
| AE-066, AE-071 | The answers are correct; the scoring patterns are too strict | Scorer limitation |

## 2. Model comparison (smoke suite, 23 cases, same prompt v4)

| Model | Passed | Median response time | Notes |
|---|---|---|---|
| gpt-4.1 | **22/23** | 2.4 s | Only failure: AE-075 (above) |
| gpt-4.1-mini | 19/23 | 2.1 s | Said "no new inquiries Oct 1–10" when there were 2, contradicted itself about a customer, and asked for details instead of checking records |

**Conclusion:** keep gpt-4.1 for the assistant. Mini costs about a fifth as much per question but makes factual
errors. The app's cost estimate currently uses gpt-4.1 prices for every model, so mini's real cost (about $0.06) is
lower than the $0.29 shown.

## 3. AI inquiry reading (127 customer messages)

| Metric | Basic rule-based reader | AI reader (gpt-4.1) |
|---|---|---|
| Field accuracy | 97.4% | **98.5%** |
| All fields exactly right | 82.7% | **84.3%** |
| Missing-info flags that were right (precision) | 82.6% | **98.9%** |
| Missing info caught (recall) | **97.9%** | 94.9% |
| Response time | 4 ms | 2.2 s |
| Cost | $0 | $0.82 |

The first AI score was 96.0% / 68.5%. Most of the gap was the AI returning "Nov 14" as an exact date **plus** an
identical Nov 14–14 window, which the scorer counted as wrong. The app now normalises that, which also cleans the
stored data. The saved AI answers were then re-scored without new API calls. Remaining AI errors are mostly
destination (7) and trip purpose (5).

**Caveat:** the basic reader's rules and these test messages were written together, so its score is optimistic. The
AI's advantage on unfamiliar, messy messages is likely larger than this table shows.

## 4. Performance at scale

A temporary database held 2,000 inquiries, 2,944 quotes, 540 bookings and 796 follow-ups. Times are the best of 3:

| Calculation | Before | After |
|---|---|---|
| Overview data (sum) | 4,208 ms | **1,529 ms** |
| Needs-attention list | 1,067 ms | 304 ms |
| Request → booking funnel | 1,177 ms | 263 ms |
| Team Inbox queue | 424 ms | 122 ms |

**Cause:** the code made a separate database query for each record's related quotes, bookings and customer (the
"N+1 query" problem). Related rows are now loaded in bulk. Further room: the Overview still computes some figures
twice per page load.

## 5. Accessibility (axe-core, WCAG 2.1 A/AA)

The muted grey text (#6E7891) had 3.8–4.4:1 contrast against the 4.5:1 minimum. It was changed to #5B6478
(5.1–5.9:1). Contrast issues went from 30 to 5. The customer landing page and the sign-in page have **no
violations**. The remaining items (date-picker placeholder colour, sidebar ARIA and list markup) are generated by
Streamlit itself.

## 6. Limitations of this evaluation

- **No held-out test set.** The same 114 cases were used to find problems and to confirm the fixes, so the latest
  numbers are optimistic. The fixes are general rules rather than per-case tweaks, but a fresh, unseen set of cases
  should be written before quoting a final figure.
- **The latest prompt (v4) was measured on the 33 regression cases and the 23 smoke cases, not on all 114.** A full
  run costs about $1.60.
- Rule-based scoring can still misjudge unusual phrasing; see AE-066 and AE-071.
- One evaluation run per configuration. AI answers vary between runs, so small differences (±2–3 cases) are within
  noise.
- Code coverage measures which lines ran during tests, not whether they behave correctly.
