# Design research: FlightDesk AI

**Method.** I used web search and fetched public pages; no browser was used to explore the products interactively.
Every observation below comes from public material: company blogs, design-system documentation and case studies. None
of it reflects hands-on use. Brand assets, logos and proprietary fonts were **not** copied. Only general principles
were adopted.

## Sources reviewed

| Source | What it says | What we took from it |
|---|---|---|
| **Linear**, "How we redesigned the Linear UI" (Linear blog) | They rebuilt the theme system on a few variables in LCH colour space. Navigation chrome (sidebar) is dimmed so the content stands out. Borders are softened and the type uses Inter Display for headings. | A few design tokens drive the whole app. The dark sidebar is quieter than the content. Borders are light and warm, never harsh. Headings use a display cut of Inter (Inter Tight). |
| **Stripe** (dashboard and marketing design writing) | Dense but calm data views, strong typographic hierarchy, and a feeling of trust from precision and restraint. | Numbers are tabular and right-aligned in tables. Each KPI card shows its comparison and how it is counted. No decorative charts. |
| **Ramp** (brand and product site) | Near-black ink on a warm sand background, a single accent colour, and a finance product that feels calm rather than "fintech neon". | Warm off-white canvas (`#FAF8F4`), near-black ink (`#0E1A2B`) and one blue accent (`#2F6BDB`). Money is shown per currency and is never decorated. |
| **Vercel Geist** design system | High-contrast, accessible colour scales; a sans and mono pair; restraint; visible focus states. | WCAG-minded text contrast on the canvas and the navy sidebar. A visible `:focus-visible` outline on every interactive element. One font family. |
| **Navan** (TripActions) rebrand case study by Pentagram, plus general CRM and travel-ops UX | Travel software used by operators should feel premium but be fast to scan. Status matters more than imagery. | Status pills with icon and text (never colour alone). The "Needs attention" list comes before charts. No stock aviation photography in the working UI. |

## Resulting design system (as implemented in `src/ui/common.py` and `.streamlit/config.toml`)

- **Colour tokens.**
  - Navy `#0B1B33` (sidebar, login brand panel) and navy-2 `#13294B`.
  - Ink `#0E1A2B`, canvas `#FAF8F4`, surface white, line `#E8E3D9` and accent `#2F6BDB`.
  - Status colours: success, warning and danger. They are never reused as chart series.
- **Charts.**
  - A fixed categorical order starting with `#2a78d6`, `#eb6834` and `#1baf7a`.
  - A single hue (`#2a78d6`) for magnitude.
  - One axis per chart, light gridlines, and hover tooltips.
- **Type.** Inter for the UI and Inter Tight for headings, loaded from Google Fonts. Numbers use `tabular-nums`.
- **Layout.**
  - A page header with title, one-line purpose and primary action.
  - KPI cards that state their comparison.
  - "Needs attention" before trends, and empty states that offer the next action.
- **Navigation.**
  - Plain-English page names: Overview, Inquiries, Quotes, Customers, Follow-ups, AI Assistant, AI Evaluation Lab,
    Reports and Settings, plus an *Advanced* section.
  - The menu shows only the pages the signed-in role can open.
- **Login.** A split layout with a navy brand panel, the product promise and a "demo / not affiliated" note, plus a
  plain form with a generic error message.
- **Accessibility.**
  - Focus outlines; status is shown with icon and text, not colour alone.
  - Every chart offers a table view or numbers in text.
  - Sufficient contrast on both surfaces.

## What was deliberately *not* done

- No imitation of Flygreen's (or any operator's) branding. The product is labelled an independent prototype.
- No dark mode. Streamlit's theme is set once in `config.toml`, and a second validated palette was out of scope.
- No custom JavaScript components. Everything is native Streamlit plus CSS, so it stays maintainable.
