# AI Daily Brief

Daily email: 5 new AI buzzwords (plain English) plus up to 6 AI news items, each labelled
**FACT / JUDGEMENT / INFERENCE / RUMOR** with sources.

## Setup
1. Add these GitHub repo secrets: `ANTHROPIC_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
   `SMTP_PASSWORD`, `MAIL_FROM`, `NEWSLETTER_RECIPIENTS` (comma-separated addresses).
   Gmail: host `smtp.gmail.com`, port `587`, and an app password.
2. Merge to the default branch. GitHub runs schedules only from there.
3. Run once via Actions > AI Daily Newsletter > Run workflow.

Change the send time in `.github/workflows/ai-newsletter.yml` (cron is UTC).

## Local preview (no email)
    pip install -r requirements.txt
    ANTHROPIC_API_KEY=... python newsletter.py --dry-run   # writes out/latest.html
    python -m pytest -q

## No repeats
Buzzwords and news already sent are stored in `state/seen.json` and never sent again. A story
returns only if the research states a material change (new facts, confirmation, correction, label
change); it then carries an UPDATE badge and a "What changed" line.

## Design
Single column, 560px max, large type and spacing for phones, card layout. Light and dark themes via
`prefers-color-scheme`, with soft pastel label pills.

## How labels are enforced
- Web search is limited to the domains in `sources.json`.
- Sources must be https, allowlisted, and actually returned by the search.
- FACT needs a primary source or 2+ independent domains, else it becomes RUMOR.
- Items without a valid source are dropped; no valid content means no email.

Model: `claude-sonnet-5-5` (override with `NEWSLETTER_MODEL`).
Limit: labels come from AI research plus these rules, not human fact-checking.
