#!/usr/bin/env python3
"""Phases A-C: source the 1,000-dev rising-MCP-contributor cohort.

A: seed repos = modelcontextprotocol org + top topic:mcp repos (>=50 stars)
B: contributor harvest across seeds (top 100 per repo, bots excluded)
C: rising filter (ecosystem commits >= 3, account >= 180 days, <= 2,000
   followers), ranked by ecosystem commits, ties broken by fewer followers.

Each phase caches to disk; delete the cache file to re-harvest.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from github_talent_mcp.github_client import GitHubClient

SEED_PATH = Path("seed_repos.json")
CONTRIB_PATH = Path("contributors_raw.json")
USERS_PATH = Path("users_raw.json")
COHORT_PATH = Path("cohort.json")

SEED_CAP = 200
MIN_SEED_STARS = 50
MIN_ECOSYSTEM_COMMITS = 3
# Damps single-repo dominance: a member's rank credit from any one seed repo
# is capped so breadth matters at the margin.
PER_REPO_COMMIT_CAP = 300
# Seeds excluded after manual review (2026-07-17), documented in cohort.json.
SEED_EXCLUSIONS = {
    "affaan-m/ECC": (
        "star-inflation signature: 230k stars on a 6-month-old repo, "
        "stargazers endpoint returns 404 (star data unverifiable, consistent "
        "with a flag for suspicious starring), single-author commit history "
        "(owner has 1,486 of ~1,700 commits), 271 contributors total"
    ),
    "Snailclimb/JavaGuide": (
        "off-topic: Java interview-prep guide; the mcp topic tag is unrelated "
        "to the repo's content or contributor base"
    ),
}
MAX_FOLLOWERS = 2000
MIN_ACCOUNT_AGE_DAYS = 180
COHORT_SIZE = 1000
CONCURRENCY = 8

# Automation accounts without a [bot] suffix. web-flow is GitHub's
# merge-commit service account and appears in contributor lists. The last
# three are type=User accounts confirmed as org automation by profile
# inspection (2026-07-17): "Netdata bot", "LobeHub Bot", and a nameless
# zero-repo account pushing 12k automated commits.
KNOWN_AUTOMATION = {
    "web-flow", "ghost", "actions-user",
    "netdatabot", "lobehubbot", "automated-commits-ap",
    # "Weblate (bot)": hosted-translation pusher, type=User, found when its
    # contribution graph terminally broke the GraphQL signal fetch (2026-07-19)
    "weblate",
}
AUTOMATION_NAME_RE = re.compile(
    r"(^|[-_])(bot|ci|cd|release|releases|deploy|automation|builder|jenkins|"
    r"travis|circleci|github-actions|renovate|dependabot|mirror|sync)($|[-_0-9])",
    re.IGNORECASE,
)


async def phase_a_seeds(gh):
    if SEED_PATH.exists():
        return json.loads(SEED_PATH.read_text())
    print("Phase A: harvesting seed repos...")
    repos = {}
    # gh._get is the package's retrying GET; used directly since the client
    # exposes no org-repos or repo-search method.
    page = 1
    while True:
        resp = await gh._get(
            "/orgs/modelcontextprotocol/repos",
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        for r in batch:
            if not r.get("fork") and not r.get("archived"):
                repos[r["full_name"]] = r.get("stargazers_count", 0)
        if len(batch) < 100:
            break
        page += 1
    org_seeds = dict(repos)

    for topic in ("mcp", "model-context-protocol"):
        for page in (1, 2):
            resp = await gh._get(
                "/search/repositories",
                params={
                    "q": f"topic:{topic} stars:>={MIN_SEED_STARS} archived:false",
                    "sort": "stars", "order": "desc",
                    "per_page": 100, "page": page,
                },
            )
            resp.raise_for_status()
            for r in resp.json().get("items", []):
                if not r.get("fork"):
                    repos[r["full_name"]] = r.get("stargazers_count", 0)
            await asyncio.sleep(2)  # search quota is 30 req/min

    topical = sorted(
        ((k, v) for k, v in repos.items() if k not in org_seeds),
        key=lambda kv: kv[1], reverse=True,
    )
    seeds = dict(org_seeds)
    for full_name, stars in topical:
        if len(seeds) >= SEED_CAP:
            break
        seeds[full_name] = stars

    data = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "org": "modelcontextprotocol (non-fork, non-archived)",
            "topics": ["mcp", "model-context-protocol"],
            "min_stars_topical": MIN_SEED_STARS,
            "cap": SEED_CAP,
        },
        "repos": [
            {"full_name": k, "stars": v}
            for k, v in sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }
    SEED_PATH.write_text(json.dumps(data, indent=2))
    print(f"  {len(data['repos'])} seed repos ({len(org_seeds)} from the org)")
    return data


async def phase_b_contributors(gh, seeds):
    if CONTRIB_PATH.exists():
        return json.loads(CONTRIB_PATH.read_text())
    print("Phase B: harvesting contributors...")
    sem = asyncio.Semaphore(CONCURRENCY)
    contributions: dict[str, dict[str, int]] = {}
    suffix_bots = 0
    failures = []

    async def one(full_name):
        nonlocal suffix_bots
        owner, name = full_name.split("/", 1)
        async with sem:
            try:
                rows = await gh.get_repo_contributors(owner, name, per_page=100)
            except Exception as exc:
                failures.append(f"{full_name}: {type(exc).__name__}")
                return
        for c in rows:
            login = c.get("login")
            if not login:
                continue
            if c.get("type") == "Bot" or login.endswith("[bot]"):
                suffix_bots += 1
                continue
            contributions.setdefault(login, {})[full_name] = c.get("contributions", 0)

    active = [r["full_name"] for r in seeds["repos"]
              if r["full_name"] not in SEED_EXCLUSIONS]
    await asyncio.gather(*(one(full_name) for full_name in active))
    data = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "seeds_used": len(active),
        "contributions": contributions,
        "failed_repos": failures,
    }
    CONTRIB_PATH.write_text(json.dumps(data))
    print(f"  {len(contributions)} unique humans across {len(active)} seeds, "
          f"{suffix_bots} [bot]-suffixed rows dropped, {len(failures)} repos failed")
    return data


async def phase_c_filter(gh, contrib):
    print("Phase C: rising filter...")
    users_cache = json.loads(USERS_PATH.read_text()) if USERS_PATH.exists() else {}
    per_repo = contrib["contributions"]
    capped = {
        login: sum(min(n, PER_REPO_COMMIT_CAP) for n in repos.values())
        for login, repos in per_repo.items()
    }
    raw = {login: sum(repos.values()) for login, repos in per_repo.items()}
    now = datetime.now(timezone.utc)

    candidates = sorted(
        ((login, n) for login, n in capped.items()
         if n >= MIN_ECOSYSTEM_COMMITS),
        key=lambda x: (-x[1], x[0]),
    )
    print(f"  {len(candidates)} candidates with >= {MIN_ECOSYSTEM_COMMITS} "
          f"ecosystem commits")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def lookup(login):
        if login in users_cache:
            return
        async with sem:
            try:
                u = await gh.get_user(login)
                users_cache[login] = {
                    "followers": u.get("followers", 0),
                    "created_at": u.get("created_at"),
                    "type": u.get("type"),
                }
            except Exception as exc:
                users_cache[login] = {"error": f"{type(exc).__name__}"}

    qualified = []
    excluded_automation = []
    rejected = {"too_famous": 0, "too_new": 0, "not_user_type": 0, "lookup_error": 0}

    # Process in commit-rank order, one tier (= same commit count) at a time,
    # so we can stop looking up users once the cohort is full without breaking
    # the follower tie-break at the boundary tier.
    idx = 0
    while idx < len(candidates):
        tier_commits = candidates[idx][1]
        tier = [c for c in candidates[idx:] if c[1] == tier_commits]
        idx += len(tier)
        await asyncio.gather(*(lookup(login) for login, _ in tier))
        for login, n in tier:
            u = users_cache[login]
            if "error" in u:
                rejected["lookup_error"] += 1
                continue
            if login in KNOWN_AUTOMATION or AUTOMATION_NAME_RE.search(login):
                excluded_automation.append(
                    {"login": login, "ecosystem_commits": n,
                     "reason": ("known automation account" if login in KNOWN_AUTOMATION
                                else "automation name pattern")})
                continue
            if u.get("type") != "User":
                rejected["not_user_type"] += 1
                continue
            created = datetime.fromisoformat(u["created_at"].replace("Z", "+00:00"))
            age_days = (now - created).days
            if age_days < MIN_ACCOUNT_AGE_DAYS:
                rejected["too_new"] += 1
                continue
            if u["followers"] > MAX_FOLLOWERS:
                rejected["too_famous"] += 1
                continue
            qualified.append({
                "login": login,
                "ecosystem_commits": n,
                "raw_commits": raw[login],
                "seed_repos_contributed": len(per_repo[login]),
                "followers": u["followers"],
                "account_age_days": age_days,
            })
        USERS_PATH.write_text(json.dumps(users_cache))
        if len(qualified) >= COHORT_SIZE:
            break

    qualified.sort(key=lambda q: (-q["ecosystem_commits"], q["followers"]))
    cohort = qualified[:COHORT_SIZE]
    data = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_ecosystem_commits": MIN_ECOSYSTEM_COMMITS,
            "max_followers": MAX_FOLLOWERS,
            "min_account_age_days": MIN_ACCOUNT_AGE_DAYS,
            "per_repo_commit_cap": PER_REPO_COMMIT_CAP,
            "rank": "capped ecosystem commits desc, followers asc tie-break",
            "excluded_seeds": SEED_EXCLUSIONS,
        },
        "funnel": {
            "candidates_with_min_commits": len(candidates),
            "user_lookups": len(users_cache),
            "rejected": rejected,
            "excluded_automation": excluded_automation,
            "qualified": len(qualified),
            "cohort": len(cohort),
        },
        "cohort": cohort,
    }
    COHORT_PATH.write_text(json.dumps(data, indent=2))
    return data


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))]


def summarize(data):
    cohort = data["cohort"]
    print("\n" + "=" * 64 + "\nCOHORT SHAPE\n" + "=" * 64)
    for key, label in (("ecosystem_commits", "capped commit credit"),
                       ("raw_commits", "raw commits"),
                       ("followers", "followers"),
                       ("account_age_days", "account age (days)"),
                       ("seed_repos_contributed", "seed repos touched")):
        vals = sorted(c[key] for c in cohort)
        print(f"{label:>20}: min {vals[0]}, p25 {pct(vals, 25)}, "
              f"median {pct(vals, 50)}, p75 {pct(vals, 75)}, "
              f"p95 {pct(vals, 95)}, max {vals[-1]}")
    print(f"\nFunnel: {json.dumps(data['funnel'], indent=2, default=str)[:1200]}")
    print("\nTop 30 by capped commit credit (for automation scan):")
    for c in cohort[:30]:
        print(f"  {c['ecosystem_commits']:>6} credit ({c['raw_commits']:>6} raw)  "
              f"{c['login']:<28} {c['followers']:>5} followers  "
              f"{c['account_age_days']:>5}d  {c['seed_repos_contributed']} repos")


async def main():
    gh = GitHubClient()
    try:
        resp = await gh._get("/rate_limit")
        core = resp.json()["resources"]["core"]
        print(f"GitHub core quota: {core['remaining']}/{core['limit']} remaining\n")
        seeds = await phase_a_seeds(gh)
        contrib = await phase_b_contributors(gh, seeds)
        start = time.time()
        data = await phase_c_filter(gh, contrib)
        print(f"  phase C took {round(time.time() - start, 1)}s")
    finally:
        await gh.close()
    summarize(data)


if __name__ == "__main__":
    asyncio.run(main())
