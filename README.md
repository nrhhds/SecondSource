# Second Source

Article-level media analysis for Florida political coverage.

**Site:** chooseyourbias.com

---

## What this is

Most media-bias tools rate *outlets*. A wire story and an opinion column from the same publication get the same label. Second Source scores *articles*, using countable features of the text itself.

The engine is open. The rubric is versioned. Every published score can be regenerated from this repository.

## What this is not

This is not an attempt at neutrality. Neutrality is not achievable and anyone claiming it is selling something. The goal is **consistency and auditability**: the same article scored twice gets the same result, the method is public, and you can check our work.

## How it works

1. **Ingest** — articles are pulled from public feeds. We never republish content. Scores link out to the original.
2. **Blind** — outlet name, byline, URL, and identifying boilerplate are stripped before scoring. The engine does not know who wrote it.
3. **Score** — articles are measured on countable signals (see [`RUBRIC.md`](RUBRIC.md)). Component scores are published, not just a composite.
4. **Publish** — the pipeline runs unsupervised and publishes unconditionally. See "Human intervention" below.

## Calibration

Three tests, re-run on every rubric version, results published:

| Test | Expectation |
|---|---|
| **Wire anchor** | News Service of Florida copy scores near-neutral. It's the wire everyone republishes. If it doesn't, the engine is wrong. |
| **Known-lean separation** | Outlets with established lean separate in the expected directions, with symmetric magnitude. Asymmetric magnitude indicates model lean, not outlet lean. |
| **Valence swap** | Flip party names and actor identities so structure is identical and political direction reverses. Scores should match. The delta is published. |

Test-retest variance is measured and published. We do not hide it.

## Human intervention

No human approves scores. The pipeline publishes unconditionally.

A score may be pulled only for enumerated mechanical reasons:

- `EXTRACTION_ERROR` — the parser failed
- `WRONG_CLUSTER` — article matched to the wrong story
- `DUPLICATE` — same article, multiple feeds
- `LEGAL` — defamation or takedown risk

**"The score seems wrong" is not a valid reason.** Every intervention is logged publicly with its reason code and timestamp.

## Output constraints

Published output contains only:
- Counts and measurements computed by this code
- Verbatim quotations with links to the source

No generated prose making factual claims about people.

## Editorial

Second Source publishes media criticism — analysis of *how* stories were covered, not advocacy on the underlying policy.

- [Recusal list](docs/RECUSAL.md)
- [Corrections policy](docs/CORRECTIONS.md)
- [Intervention log](docs/INTERVENTIONS.md)
- [Editor's disclosed priors](docs/PRIORS.md)

## Reproducing a score

```bash
pip install -r requirements.txt
python score.py --url "<article url>" --rubric-version 0.1
```

Every published score is stamped with the rubric version used. Old scores remain reproducible against their original rubric.

## License

Engine: MIT. Analysis output: CC BY 4.0.
