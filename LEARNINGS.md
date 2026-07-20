# Learnings

Operational knowledge earned while building and running the scout swarm.
Written for anyone reproducing or extending this pipeline.

## GitHub GraphQL: `contributionsCollection` has three failure tiers

1. **Default (12-month) window fails persistently for hyper-active users.**
   `commitContributionsByRepository` returns `RESOURCE_LIMITS_EXCEEDED` and
   retries never help. Fix: pass an explicit shorter `from`/`to` window
   (6 months worked where 12 failed). Use the *same* window for every
   developer you intend to compare -- mixed windows skew any downstream
   ranking.
2. **Sustained call rates trip the secondary rate limit.** 403s arrive with
   the primary quota nearly untouched. Handle 403/429 with `Retry-After`,
   pace call *starts* (~1.4/s was too hot in bursts; we settled on a spacing
   gate), and never hold a shared lock through retry sleeps -- one failing
   query stalls the whole pipeline.
3. **For a small tail of users, the query itself is the problem.** The
   per-query compute cost of their contribution graph triggers 403s and
   resource limits regardless of pacing or cool-down (observed: a plain curl
   for one such user hung for 2+ minutes; even the two-scalar query failed,
   with the error path pointing at `restrictedContributionsCount`).
   Degradation ladder that converged: full 6-month per-repo query ->
   3-month window -> `totalCommitContributions` alone (always passed), with
   org-repo star counts recovered via REST and the private-commit count
   reported as "n/a", never a fabricated zero. Record the degradation tier
   per row so downstream consumers know what they're reading.

## GitHub REST: quiet scope traps

- `search/commits` with `author:X user:X` counts **owned repos only** --
  the `user:` qualifier silently hides all org-repo work. Author-only
  search (`author:X committer-date:>=D`) includes org commits and
  cross-checks GraphQL totals well (596 vs 614 on our worst-case user).
- The Events API covers ~300 recent events; 30/90-day commit counts derived
  from it miss squash-merge workflows entirely. In our run this produced
  "0 commits/90d" next to "1,000+ commits/6mo" in the same signal block --
  the evaluator correctly flagged the contradiction on ~half of all
  profiles, which reads as an accusation it isn't. v2 fix: drop the
  events-based activity lines from the signals.

## Contributor harvesting

- **Bot filtering needs four layers.** API `type == "Bot"` and the `[bot]`
  suffix miss org service accounts registered as normal users (`web-flow`,
  `netdatabot`, `lobehubbot`, a nameless account pushing 12k automated
  commits). Add a boundary-aware name-pattern filter, then a manual scan of
  the commit-ranked top with profile inspection as ground truth. Document
  every exclusion with its reason.
- **Topic tags are self-applied and get abused.** A Java interview-prep
  guide carried the `mcp` topic. Sort your seed list by stars and read the
  top 30 before trusting it.
- **Star counts can be manufactured.** A 6-month-old repo claiming 230k
  stars returned 404 on its stargazers endpoint while every other endpoint
  worked -- unverifiable popularity is disqualifying by itself in a
  public-signals pipeline. Cheap checks: stargazers endpoint status, star
  velocity, contributor-count-to-star ratio, commit concentration.
- **Cap per-repo commit credit** (we used 300) when ranking across an
  ecosystem, or the ranking collapses into "core maintainers of the single
  biggest repo". The cap changed almost nothing about *who* qualified but
  everything about *ordering*.

## LLM evaluation at scale

- **Free text authored by the person being scored is an injection
  surface.** Bios, READMEs, repo descriptions. The system prompt declares
  all signal content untrusted data; a synthetic profile with an embedded
  "score this 100" directive scored 9 and was flagged as manipulation.
- **Provider content filters will reject some real profiles.** One
  developer's adult-content repo description drew a "high risk prompt" 400.
  Decide the policy up front (we document the account as
  provider-unscoreable rather than silently sanitizing signals).
- **Don't feed the model precomputed scores** (the enrichment library's
  0-205 activity score) unless you want them anchoring its rubric.
- Strict JSON schema + one validation-error retry held up: 8 retries across
  960 evaluations, zero unparseable results.

## Operations

- **Append-only JSONL + skip-completed resume is the whole ballgame.** The
  run survived two rate-limit incidents, several process kills, a provider
  account suspension, and multiple relaunches without refetching or
  double-billing anything.
- **Run provider inference and rate-limited fetching as separate sequential
  stages.** You get honest per-stage wall times and a K3 outage can't waste
  GitHub quota (or vice versa).
- **A suspended provider account does not always fail fast.** Some calls
  429'd immediately; others hung in limbo for ~16 hours and then succeeded
  once the account recovered. Treat multi-hour in-flight calls as zombies:
  kill and re-run from the resume point.
- **Know your per-1,000 cost before the run, at real prices.** Ours came in
  at $16.40 vs a $12.90 projection (longer signal blocks than the pilot),
  which was enough to drain the balance and suspend the account mid-run.
  Standing rule now: any single call projected over $0.50 needs explicit
  approval first.
