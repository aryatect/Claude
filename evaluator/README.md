# Unbiased Evaluator

A standalone Claude API agent that judges a claim, news story, workplace situation or technical
finding on evidence and plausibility. It judges each part and the whole, and it treats two or more
coincidences as a warning sign.

## Run
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...
    python evaluator.py "The outage started 2 minutes after the deploy, and the on-call was away."
    python evaluator.py --file case.txt --json out/verdict.json
    python evaluator.py --offline "..."   # no web search; nothing leaves except the API call
    python -m pytest -q                   # rule tests, no API calls

Env overrides: `EVALUATOR_MODEL` (default `claude-opus-5-5`), `EVALUATOR_EFFORT` (default `high`),
`EVALUATOR_MAX_SEARCHES` (default `15`).

## How it works
| Step | What happens |
|---|---|
| 1. Research | Claude restates the subject neutrally, splits it into atomic claims and searches the web for evidence for **and** against each one. |
| 2. Adjudicate | Claude returns JSON that must match a strict schema: claims, coincidences, competing hypotheses, bias checks and the whole verdict. |
| 3. Enforce | Python rules override the model wherever the evidence does not support it. Each override is listed in the output. |

## Hard rules (in `enforce()`, covered by tests)
| Rule | Effect |
|---|---|
| Source must be a URL the search really returned, or `input` | Other sources are dropped. |
| A supported FACT needs a retrieved source | Otherwise it becomes UNVERIFIED with LOW confidence. Offline FACT claims are always UNVERIFIED. |
| HIGH confidence needs 2+ independent domains and no counter-evidence | Otherwise it is capped at MEDIUM. |
| A load-bearing claim is CONTRADICTED | The whole cannot be SUPPORTED; it becomes UNSUPPORTED. |
| A load-bearing claim is UNVERIFIED or MIXED | A SUPPORTED whole becomes PARTLY_SUPPORTED. |
| Weakest link | The whole's confidence is no higher than its weakest load-bearing claim. |
| 2+ coincidences (cluster) | Flagged; the whole's confidence is capped at MEDIUM. |
| The top hypothesis needs 2+ coincidences to be chance | The whole's confidence drops to LOW. |
| Fewer than 2 hypotheses tested | The whole's confidence drops to LOW. |

## Method sources
- Atomic-claim checking: Min et al., *FActScore* (EMNLP 2023), https://arxiv.org/abs/2305.14251
- Competing hypotheses, ranked by inconsistent evidence: Heuer, *Psychology of Intelligence Analysis*,
  ch. 8 (CIA, 1999), https://www.cia.gov/resources/csi/static/Pyschology-of-Intelligence-Analysis.pdf
- Why a cluster of coincidences is unlikely (conjunction rule): Tversky & Kahneman (1983),
  *Psychological Review* 90(4), doi:10.1037/0033-295X.90.4.293

## Limits
No model is fully unbiased. The rules limit overconfidence; they do not remove model or search-result
bias. Coincidence probabilities are the model's estimates. Use `--offline` for private workplace
details, because web search sends queries outside your organisation.

## Skill version
The same method is packaged as a Claude skill in `.claude/skills/unbiased-evaluator/`. Claude Code
loads it automatically in this repo. Its `scripts/enforce.py` applies the same hard rules; the
tests run every rule against both copies so they cannot drift apart.
