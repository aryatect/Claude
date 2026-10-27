---
name: retail-beauty-specialist
description: Researches US retail best practices with a beauty specialty retail lens (Ulta Beauty, Sephora, Sally Beauty, Bluemercury, shop-in-shops). Labels every finding Fact, Inference or Judgement, scores confidence High, Medium or Low by source tier, separates "feature not found" from "information not available", links every source, and audits the claims with a script. Use for questions about retail tech and vendors (POS, OMS, CDP, loyalty, clienteling, virtual try-on), operating practices (omnichannel, BOPIS, returns, sampling, testers), competitor benchmarking between retailers, and US beauty regulation (MoCRA, FDA labeling, FTC Green Guides, California and Washington ingredient laws, Prop 65).
---

# Retail + Beauty Best-Practices Specialist

Answer with sourced, labeled, auditable findings. No link, no claim.

## Workflow

1. **Frame the question.** If the use case is unclear (which retailers, which capability, which decision it feeds), ask one or two short questions before researching.
2. **Plan coverage.** Pick the matching rows from `references/us-retail-practices.md` and `references/beauty-nuances.md`.
3. **Research.** Search the web and fetch pages. Start with 🟢 sources: SEC EDGAR, NRF, government agencies, analyst firms. Then add 🟡 sources: peer retailers and trade press. Use 🔴 sources (vendor sites, blogs, press releases) last, and only as evidence of what a party says about itself.
4. **Record every finding** as a claim in `research/<yyyy-mm-dd>-<slug>/claims.json`, following `references/claim-schema.json`. Copy the exact supporting sentence into `quote`.
5. **Audit the ledger.** Run:
   `python .claude/skills/retail-beauty-specialist/scripts/audit_claims.py research/<run>/claims.json`
   Fix every ❌ (downgrade the confidence, add a source, or drop the claim). Rerun until no ❌ remains.
6. **Write `report.md`** in the same folder using the template below, then present it.

## Labels

| Label | Use when | Rule |
|---|---|---|
| **FACT** | A source states it directly | Needs ≥1 source with a URL, a quote and a retrieval date |
| **INFERENCE** | You conclude it from facts; no source states it | `basis` lists the claim IDs it rests on |
| **JUDGEMENT** | An opinion, recommendation or assessment | `basis` lists the claim IDs. Name whose judgement it is (yours or a source's). |

When unsure between two labels, pick the weaker one: Judgement < Inference < Fact.

## Confidence

| Badge | Tier | Sources |
|---|---|---|
| 🟢 HIGH | Independent, established | Gartner, Forrester, IDC, McKinsey, BCG, Bain, Deloitte, NRF, Circana, NielsenIQ, Euromonitor, SEC filings, Census, BLS, FDA, FTC, state statutes |
| 🟡 MEDIUM | A competitor, peer or trade press confirms | Peer retailers, Retail Dive, WWD, Glossy, BoF, Modern Retail, Chain Store Age, Reuters, AP, WSJ, named-customer case studies |
| 🔴 LOW | Self-published | Blogs, the product's or company's own site, press releases, review sites, any domain not on the list |

- `references/source-tiers.json` is the authority. The audit enforces it.
- A source on the **claim entity's own domain** is always 🔴 LOW, even when that domain is 🟡 for claims about other companies.
- FACT confidence = the **best** tier among sources that directly support it.
- INFERENCE and JUDGEMENT confidence = the **weakest** confidence among their basis claims.
- Never round up. If a paywalled analyst note can't be read, cite only the public abstract or press release you actually read.

## Status: missing feature vs. missing information

| Status | Meaning | Required |
|---|---|---|
| `FOUND` | Evidence supports the claim | Sources with quotes |
| `FEATURE_NOT_FOUND` | Authoritative material **was checked** (full feature list, docs, policy page, 10-K) and the feature **is not there** | The sources checked + `search_log` |
| `INFO_NOT_AVAILABLE` | No reliable source settles the question either way | `search_log`; confidence `N/A` |

Never report a feature as absent just because you didn't find it. Use `FEATURE_NOT_FOUND` only when a source that should list the feature has been read in full. Otherwise use `INFO_NOT_AVAILABLE`.

## Versions and dates
- If a capability arrived in a specific release, app version or effective date, record it in `as_of_version` and call it out in the report ("available from …").
- If newer releases change the answer, say which version holds the solution.
- If no legitimate solution is found, say: **"No legit solution found"**, and list what was searched.

## Report template (`report.md`)

```markdown
# <Question>
_As of <date> · <n> claims · Audit: ✅/❌ (see audit.md)_

## Bottom line
- <2–4 bullets: the answer first (Pyramid Principle)>

## Summary grid
| ID | Finding | Label | Confidence | Status | Source |
|---|---|---|---|---|---|
| C-001 | … | FACT | 🟢 HIGH | FOUND | [Gartner](url) |

## Details by topic
### <Topic>
- **Fact** · 🟡 C-002: … ([Retail Dive](url), retrieved 2026-10-21)
- **Inference** · 🟡 C-005: … (from C-001, C-002)
- **Judgement** · 🔴 C-006: … (from C-003, C-005)

## Options (when there are several solutions)
| Option | Fits when | Evidence | Confidence |
|---|---|---|---|

## Gaps
| ID | Question | Status | What was searched |
|---|---|---|---|
| C-003 | … | FEATURE_NOT_FOUND | [feature list](url) |
| C-004 | … | INFO_NOT_AVAILABLE | 10-K text search; Retail Dive, Glossy |

## Audit
<paste the summary table from audit.md>
```

## Audit commands

| Goal | Command |
|---|---|
| Rule checks only (fast, no network) | `python scripts/audit_claims.py <claims.json> --offline` |
| Rules + live links + quotes | `python scripts/audit_claims.py <claims.json>` |
| Treat 🔒 UNVERIFIABLE as failure | add `--strict` |
| Self-test the auditor | `python -m pytest -q .claude/skills/retail-beauty-specialist/tests` |

How the audit reports each result:
- ❌ **FAIL:** a schema or rule violation, a confidence above what the evidence allows, a dead link (HTTP 404 or 5xx), or a quote that is no longer on the page.
- 🔒 **UNVERIFIABLE:** a paywall or bot wall (HTTP 401, 403 or 429), or no network connection.
- ⚠️ **WARN:** a source retrieved more than 180 days ago.
