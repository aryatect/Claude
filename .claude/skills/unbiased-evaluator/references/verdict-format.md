# Verdict JSON format

`scripts/enforce.py` reads this shape. Every key is required; arrays may be empty.
Enum values are exact and upper-case.

```json
{
  "neutral_restatement": "Plain, neutral version of the subject.",
  "claims": [
    {
      "id": "C1",
      "text": "The deploy finished at 14:02 UTC.",
      "label": "FACT",                      // FACT | INFERENCE | JUDGEMENT
      "load_bearing": true,                 // does the story fail if this is false?
      "verdict": "SUPPORTED",               // SUPPORTED | CONTRADICTED | MIXED | UNVERIFIED | NOT_CHECKABLE
      "confidence": "HIGH",                 // HIGH | MEDIUM | LOW
      "evidence_for": [
        {"summary": "Deploy log shows completion at 14:02.", "source": "input"}
      ],
      "evidence_against": [
        {"summary": "Status page says 14:10.", "source": "https://status.example.com/incidents/123"}
      ],
      "reasoning": "Why this verdict."
    }
  ],
  "coincidences": [
    {
      "description": "Outage began 2 minutes after the deploy.",
      "claim_ids": ["C1", "C2"],
      "estimated_chance": "about 1 in 30 for an unrelated failure in that window",
      "independent_of_others": true
    }
  ],
  "hypotheses": [
    {
      "name": "Deploy caused it",
      "explanation": "The new config broke the connection pool.",
      "inconsistent_evidence": ["Errors started in a region the deploy had not reached."],
      "depends_on_coincidences": 0,         // coincidences this needs to be pure chance
      "rank": 1                             // 1 = most consistent with the evidence
    }
  ],
  "bias_checks": {
    "loaded_language": ["\"careless\" engineer"],
    "missing_perspectives": ["The on-call engineer's account"],
    "source_independence": "Two articles both quote the same company blog post: counted as one source."
  },
  "whole": {
    "verdict": "PARTLY_SUPPORTED",          // SUPPORTED | PARTLY_SUPPORTED | UNSUPPORTED | CONTRADICTED | INSUFFICIENT_EVIDENCE
    "confidence": "MEDIUM",
    "summary": "Two sentences: the answer first.",
    "coherence_issues": ["Timeline gap between 14:02 and 14:10."],
    "what_would_change_my_mind": ["Region-level error logs from 14:00 to 14:15."]
  }
}
```

The `//` comments above are explanations only; real JSON must not contain them.

`source` is either an exact URL you retrieved during research, or `input` for the user's own text.
