---
name: Deep Web Research
description: Answer a research question rigorously with sourced, cross-checked findings.
---

# Deep Web Research

Use this when the user wants a thorough, trustworthy answer to a question that needs
current or non-obvious information — not a quick fact.

## Steps

1. **Frame it.** Restate the question as 2–4 concrete sub-questions you must answer.
2. **Search broad, then narrow.** Call `search_web` for each sub-question. Prefer
   primary sources (official docs, filings, papers) over aggregators.
3. **Open the best sources.** Use `browse` on the 2–3 most promising results to read
   the actual content — never rely on a search snippet alone for a claim that matters.
4. **Cross-check.** Any load-bearing claim needs agreement from at least two
   independent sources. If sources disagree, say so and explain which you trust and why.
5. **Synthesize.** Answer the original question directly first, then the supporting
   detail. Attribute each non-obvious claim to its source.
6. **Flag gaps.** State explicitly what you could not verify or what is contested.

## Output

- Lead with the answer, not the process.
- End with a short **Sources** list (title — what it supported).
- If nothing reliable was found, say that plainly rather than padding.
