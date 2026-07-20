# Full-Run Summary: 1,000-Developer Scoring Run

The complete record of the production run — timeline, incidents, final
numbers, and per-row data-quality accounting. Methodology lives in
README.md; generalizable lessons in LEARNINGS.md.

## Outcome

| | |
|---|---|
| Cohort | 1,000 rising MCP-ecosystem contributors (`cohort.json`) |
| Scored | **999 / 1,000** |
| Unscored | 1 (provider content filter; documented, not retried) |
| K3 cost | **$17.09** (930,806 input / 952,955 output tokens, uncached rates: $3/M in, $15/M out) |
| JSON retries | 8 across 999 evaluations; zero unparseable results |
| K3 inference wall time | ~10 min total (554.8s main 960-dev pass at concurrency 50, plus completion passes) |
| GitHub fetch wall time | 58 min main pass (945 devs); recovery passes for the last ~40 devs spread over two further days |
| Band histogram | 90–100: **3** · 70–89: **115** · 50–69: **390** · 30–49: **331** · 1–29: **160** |

Top score was 90. Nobody reached the 95+ "era-defining" band — expected,
since the ≤2,000-follower ceiling excludes established names by design.

## Timeline

**2026-07-17.** Cohort built (phases A–C: 200 seeds → 8,875 humans → 4,641
candidates → 1,000 selected). First run launched. Two same-day rebuilds
after review: per-repo commit credit capped at 300 (reordered the top of
the ranking toward ecosystem breadth; only 4 members changed) and two seeds
excluded (star-inflation signature; off-topic topic tag).

**2026-07-17, first launch.** Failed fast: 161/200 fetches died on GraphQL
403s — GitHub's *secondary* rate limit, with primary quota untouched.
Killed, patched (Retry-After honored, GraphQL call starts paced), relaunched.

**2026-07-17–18, main pass.** 945 devs fetched in 58 min with failure rate
falling as the heavy-contribution-graph cluster at the top of the ranked
cohort passed. 960 devs scored in 9.2 min. Two external process kills
(cause outside the pipeline) were absorbed by the append-only resume design;
the run finished detached from the task registry.

**2026-07-18, account suspension.** The $16.40 spend drained the Moonshot
balance mid-completion; the account suspended. Notably, in-flight calls did
not all fail fast: some hung ~16 hours and then succeeded once the account
recovered after top-up. The zombie process was killed and re-run cleanly.

**2026-07-18–19, the stubborn tail.** ~40 devs failed GraphQL at every
pacing and cool-down. Root cause: per-query compute cost of their
contribution graphs — the error path eventually implicated the
`restrictedContributionsCount` aggregation itself. Resolved with a
degradation ladder (full 6-month per-repo query → 3-month window →
`totalCommitContributions` alone), converging over several free passes.

**2026-07-19, final pieces.** `weblate` — in the cohort, terminally
unfetchable — turned out to be "Weblate (bot)", a `type: User` automation
account; excluded (documented), replaced by the next qualified candidate.
`bryan-anthropic` got persistent GraphQL 502/403s at every tier; his
contribution total came from the verified REST author-only commit-search
substitute. Final dev scored; run complete.

## Data-quality accounting (all recorded per row in the results)

| Marker | Count | Meaning |
|---|---|---|
| Full-fidelity signals | 924 | 6-month per-repo GraphQL contribution data |
| 3-month window (`contrib_window_months: 3`) | 41 | Heavy graphs; labeled in the signals text |
| Degraded scalar block (`contrib_degraded: true`) | 35 | Commit total only; org-repo stars via REST; private count "n/a" |
| REST-substituted (`contrib_source`, within the 35) | 1 | GraphQL refused at all tiers; author-only commit search |
| Provider-unscoreable | 1 | Moonshot content filter ("high risk" prompt) |

Known signal artifact: events-based 30/90-day activity lines contradict
GraphQL 6-month totals for squash-merge workflows; the evaluator flags the
inconsistency, so ~half of all flags are this artifact. v2 drops those lines.

## Exclusion registry (details in `cohort.json`)

- **2 seed repos**: one star-inflation signature (unverifiable stargazers,
  single-author history), one off-topic self-tagged repo.
- **12 automation accounts**, four of them `type: User` service accounts
  that pass API-level bot filters (`web-flow`-class, `netdatabot`,
  `lobehubbot`, `automated-commits-ap`, `weblate`).

## Cost accounting

- Pilot projection: $12.90 / 1,000 devs. Actual: $17.09 — full-cohort
  signal blocks run longer than the pilot average (org-contribution
  sections, README excerpts).
- Overrun consequence: mid-run account suspension. Standing rule since:
  any single call projected over $0.50 requires explicit approval first.
- GitHub API cost: $0 (≈11k REST + ≈1.2k GraphQL calls across all passes,
  within free quotas).

## Integrity checks on the final board

- Every developer scored exactly once — no re-rolls (verified: zero
  usernames with multiple ok rows).
- No automation accounts and no injection/manipulation flags in the top 50.
- The five private validation accounts from the pilot are not cohort
  members and appear in no run artifact outside `private-data/`.
