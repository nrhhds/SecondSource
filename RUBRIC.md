# Scoring Rubric

**Version:** 0.1
**Status:** draft — not yet calibrated
**Last changed:** 2026-08-17

Every published score is stamped with the rubric version used to produce it. Changes bump the version. Old scores are not retroactively rewritten.

---

## Principle

Score **observable, countable features of the text.** Not "how biased does this feel."

A number derived from countable things can be argued on the merits. A vibe score cannot be defended, only asserted.

Where a signal requires model judgment rather than counting, it is marked **[J]** and its confidence is reported separately.

---

## Preprocessing

Before any signal is computed, the following are stripped:

- Outlet name and any masthead references
- Byline and author bio
- Source URL and domain
- Boilerplate footers, subscription prompts, related-links blocks

**The scorer never sees who wrote the article.**

---

## Signals

### 1. Sourcing

| Signal | Measure |
|---|---|
| `named_sources` | Count of distinct named, attributable human sources |
| `anon_sources` | Count of sources cited without a name |
| `source_ratio` | `named / (named + anon)` |
| `stakeholder_categories` | Count of distinct categories quoted: sponsor, opponent, agency staff, affected party, independent expert, leadership |
| `adverse_party_contacted` | Was the adversely-affected party quoted, or explicitly noted as declining? (bool) |

### 2. Quote balance

| Signal | Measure |
|---|---|
| `quote_words_by_side` | Total quoted words attributed to each identified side |
| `quote_asymmetry` | `abs(side_a - side_b) / total_quoted_words` |
| `first_opposing_quote_position` | Paragraph index of the first quote from the non-favored side |

### 3. Document grounding

| Signal | Measure |
|---|---|
| `primary_doc_links` | Count of links to bills, staff analyses, filings, official records |
| `attributed_claims` | Claims traceable to a named source or document |
| `asserted_claims` | Factual claims made in the reporter's own voice with no attribution |
| `attribution_rate` | `attributed / (attributed + asserted)` |
| `unsourced_numbers` | Numeric claims with no cited origin |

### 4. Language

| Signal | Measure |
|---|---|
| `valenced_adjectives_by_side` | Count of positive/negative modifiers applied to each side, per 100 words |
| `adjective_asymmetry` | Difference in valence loading between sides — **the asymmetry is the signal; raw count is noise** |
| `attribution_verbs_by_side` | Distribution of neutral (`said`, `stated`) vs. loaded (`claimed`, `admitted`, `insisted`, `conceded`) verbs, per side |
| `agency_obscuring_passive` | Passive constructions that remove an actor from a consequential claim |

### 5. Structure

| Signal | Measure |
|---|---|
| `opposing_view_paragraph` | Paragraph index where the opposing position first appears; `null` if absent |
| `opposing_view_share` | Proportion of total word count given to the opposing position |
| `headline_body_congruence` **[J]** | Does the headline claim more than the body supports? |
| `steelman_quality` **[J]** | Is the opposing case presented in its strongest form, or a weak proxy? |

### 6. Omission (cross-article, requires a story cluster)

| Signal | Measure |
|---|---|
| `cluster_claim_union` | Set of atomic claims across all articles on the story |
| `claims_present` | Which union claims this article carries |
| `claims_absent` | Which it omits |
| `weighted_omission` | Omissions weighted by how many outlets carry the claim. A claim in 80% of coverage and missing here is a strong signal. A single outlet's exclusive is not an omission. |

---

## Reporting

**Publish components. The composite is optional and secondary.**

The composite score is what people argue about. The components are what makes the argument winnable. Every score displays its receipts inline: the highlighted phrases, the counts, the paragraph markers.

---

## Known limitations

- Signals 5 and 6 require model judgment or clustering; both are more fragile than the counting signals.
- Omission analysis requires 3+ outlets on a story. Single-outlet stories get sourcing and language signals only.
- Paywalled outlets yield headline and dek only. These are excluded from full scoring and marked as such.
- The rubric is US/Florida-specific in its stakeholder categories.

---

## Changelog

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-08-17 | Initial draft. Uncalibrated. |
