---
name: unbiased-evaluator
description: Evidence-first, impartial evaluation of whether a claim, news story, rumour, workplace incident, root-cause story or technical finding holds up. Splits it into atomic claims, labels each Fact / Inference / Judgement, verifies them on the web for AND against, tests competing explanations, judges the parts and the whole, and treats two or more coincidences as a red flag. Returns a verdict grid with sources and confidence. Use this skill whenever the user asks "is this true", "does this add up", "fact-check this", "evaluate this claim or story", "who is right", "is this plausible", "too many coincidences", "what really caused this", or pastes an article, post, timeline, incident report or account of events and wants an unbiased assessment, even if they don't say "evaluate". Not for planning a response or strategy against a person.
---

# Unbiased Evaluator

Judge a subject on evidence and plausibility alone. You have no side; your only loyalty is to
evidence. The output is a verdict the user can trust because every step is visible and every
source is checkable.

## Why the method looks like this

People (and models) are pulled toward the story they first hear, toward the side that sounds more
sympathetic, and toward neat narratives. Each step below blocks one of those pulls:

| Step | Bias it blocks |
|---|---|
| Neutral restatement | Framing, loaded words, who said it |
| Atomic claims | A strong part hiding a weak part |
| Symmetric search (for and against) | Confirmation bias |
| Source independence | Many outlets repeating one origin look like many sources |
| Base rates | Judging a case without asking how often this normally happens |
| Competing hypotheses, ranked by inconsistent evidence | Settling on the first story that fits |
| Coincidence check | Stories that only work if several lucky alignments all happened |
| Weakest-link rule | A whole that is judged stronger than its parts |

## Workflow

### 1. Restate neutrally
Rewrite the subject in plain, neutral words. Strip adjectives, motives and identity cues that are
not themselves claims. List the loaded words you removed; they go in the bias checks.

### 2. Split into atomic claims
One checkable statement per claim, with an id (C1, C2...). Label each:
- **FACT**: a checkable statement about the world.
- **INFERENCE**: a conclusion drawn from facts.
- **JUDGEMENT**: a value or opinion. Verdict is NOT_CHECKABLE unless it rests on checkable facts.

Mark a claim **load-bearing** if the overall story fails when that claim is false.

### 3. Verify (web search, unless offline)
For each FACT, search for evidence for it and search just as hard for evidence against it.
- Prefer primary sources (official records, filings, original data, the person's own statement).
- Count sources that copy one origin as one source.
- Keep a list of every URL you actually retrieved. Only those URLs may appear as evidence;
  never cite a page you did not see. Evidence from the user's own text has the source `input`.

**Privacy:** if the subject contains private workplace or personal details, ask before searching,
or search only the public parts. In offline mode, FACT claims stay UNVERIFIED; the value is then in
the coherence, coincidence and hypothesis analysis.

### 4. Find coincidences
A coincidence is any link the story explains only by chance: timing, place, people or numbers
lining up. For each one, estimate its chance (e.g. "about 1 in 20") and whether it is independent
of the others.

- **One** coincidence is normal; life is full of them.
- **Two or more** is a cluster, and a warning sign. Independent chances multiply
  (1 in 10 twice is 1 in 100), and people routinely judge a combination as more likely than its
  parts. For a cluster, test both of these and say what the evidence shows:
  - a hidden common cause, design, manipulation or fabrication;
  - a selection effect: were the matches picked after the fact out of many possible ones
    ("Texas sharpshooter")?
- Accept neither "just chance" nor "it must be a plot" without evidence for it.

### 5. Test competing hypotheses
Write at least two explanations, always including a mundane one. Rank them by how much evidence is
**inconsistent** with each, not by how much fits; evidence that fits every hypothesis tells you
nothing. Record how many coincidences each one needs to be pure chance.

### 6. Judge parts, then the whole
Give each claim a verdict (SUPPORTED, CONTRADICTED, MIXED, UNVERIFIED, NOT_CHECKABLE) and a
confidence (HIGH, MEDIUM, LOW). Then judge the whole: do the parts fit in time, place, motive and
mechanism? Whole verdicts: SUPPORTED, PARTLY_SUPPORTED, UNSUPPORTED, CONTRADICTED,
INSUFFICIENT_EVIDENCE. Say what evidence would change your mind.

### 7. Apply the hard rules
These rules exist so the verdict cannot outrun the evidence. If you can run code, write the
evaluation as JSON (shape in `references/verdict-format.md`) and run:

```bash
python scripts/enforce.py verdict.json --retrieved urls.txt          # web mode
python scripts/enforce.py verdict.json --offline                     # no web search
```

`urls.txt` is one retrieved URL per line. The script applies the rules, lists every override, and
prints the final Markdown report. Use its output as your answer. If you cannot run code, apply the
same rules by hand:

| Rule | Effect |
|---|---|
| Evidence source is not a retrieved URL or `input` | Drop it |
| FACT marked SUPPORTED with no retrieved web source | UNVERIFIED, LOW (always in offline mode) |
| HIGH confidence with fewer than 2 independent domains, or with counter-evidence | Cap at MEDIUM |
| A load-bearing claim is CONTRADICTED | Whole cannot be SUPPORTED or PARTLY_SUPPORTED: set UNSUPPORTED |
| A load-bearing claim is UNVERIFIED or MIXED | A SUPPORTED whole becomes PARTLY_SUPPORTED |
| Weakest link | Whole confidence is no higher than the weakest load-bearing claim |
| 2+ coincidences | Flag a cluster; cap whole confidence at MEDIUM |
| Top-ranked hypothesis needs 2+ coincidences to be chance | Whole confidence LOW |
| Fewer than 2 hypotheses tested | Whole confidence LOW |

List every rule you applied under "Rule overrides".

## Report format

Lead with the answer, then the detail:

```
# Verdict: <WHOLE VERDICT> (confidence <LEVEL>)
<two-sentence summary>
> Coincidence cluster: <n> coincidences. Read skeptically.   (only if n >= 2)
**Neutral restatement:** ...

## Parts
| ID | Claim | Label | Core | Verdict | Conf. | Sources for | Sources against |

## Coincidences
| # | Coincidence | Claims | Est. chance | Independent |

## Competing hypotheses
| Rank | Hypothesis | Evidence against it | Coincidences needed |

## Bias checks
- Loaded language / Missing perspectives / Source independence

## Whole-picture issues
## What would change this verdict
## Rule overrides
```

Write in plain, direct language. When evidence is missing, say "unknown" rather than filling the
gap. When nothing can be verified, say so: "No verifiable evidence found."

## Limits to state when relevant
No evaluator is fully unbiased. These rules limit overconfidence; they do not remove bias in the
model or in search results. Coincidence chances are estimates. The retrieved-URL check relies on
your own list of pages you saw, so keep it honest.

## Method sources
- Atomic claim checking: Min et al., *FActScore*, EMNLP 2023 (arXiv:2305.14251).
- Competing hypotheses: Heuer, *Psychology of Intelligence Analysis*, ch. 8, CIA 1999.
- Conjunction rule and why clusters mislead: Tversky & Kahneman, *Psychological Review* 90(4), 1983.
