#!/usr/bin/env python3
"""Full-run scorer for the 1,000-dev cohort, in two timed stages.

Stage 1 (GitHub-paced): trimmed signal fetch -> signals_full.jsonl
Stage 2 (K3 inference): evaluate -> scout_results_full.jsonl

Both stages are resume-safe: rerunning skips usernames already completed.
Trims vs the pilot fetch: languages from repo-level `language` fields (no
per-repo byte calls), 2 event pages, and no Search-API fallback -- the
GraphQL contributions call covers org commits, and search's 30 req/min
quota would bottleneck a 1,000-dev run.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from github_talent_mcp.github_client import GitHubClient
from openai import AsyncOpenAI

from scout_pilot import (
    BASE_URL, GRAPHQL_URL, MODEL, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M,
    build_signals, evaluate, fetch_recent_contributions,
)

# Scalars only: for a handful of devs, commitContributionsByRepository is so
# expensive that GitHub 403s/resource-limits the query itself regardless of
# pacing. This minimal form always passes; org-repo stars for those devs are
# recovered via REST from their events-derived contribution list instead.
MINIMAL_CONTRIB_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
    }
  }
}
"""

COHORT_PATH = Path("cohort.json")
SIGNALS_PATH = Path("private-data/signals_full.jsonl")
RESULTS_PATH = Path("private-data/scout_results_full.jsonl")
LEADERBOARD_PATH = Path("private-data/leaderboard_full.txt")

GH_CONCURRENCY = 5
K3_CONCURRENCY = 50
# Seconds between GraphQL call starts. Raise (e.g. GQL_SPACING=5) for gentle
# mop-up passes when the token's secondary-limit window is hot.
GQL_SPACING = float(os.environ.get("GQL_SPACING", "0.7"))
EVENT_PAGES = 2
QUOTA_CHECK_EVERY = 25
QUOTA_FLOOR = 250
# Rejected by Moonshot's content filter (adult-content repo description in the
# signals); documented as provider-unscoreable rather than retried or sanitized.
PROVIDER_UNSCOREABLE = {"Andrei199991"}
BANDS = [(90, 100), (70, 89), (50, 69), (30, 49), (1, 29)]


def load_done(path, key="username"):
    done = {}
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("ok"):
                done[row[key]] = row
    return done


def append_row(path, row):
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


async def fetch_profile_trimmed(gh, username):
    user = await gh.get_user(username)
    now = datetime.now(timezone.utc)
    created = user.get("created_at")
    age_days = 0
    if created:
        age_days = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).days

    repos = [r for r in await gh.get_user_repos(username) if not r.get("fork")]
    lang_counts = {}
    for r in repos:
        lang = r.get("language")
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
    total_lang = sum(lang_counts.values()) or 1
    breakdown = {
        lang: round(count / total_lang, 3)
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
    }
    top_by_stars = sorted(repos, key=lambda r: r.get("stargazers_count", 0),
                          reverse=True)
    notable = [
        {
            "name": r["name"],
            "description": r.get("description"),
            "stars": r.get("stargazers_count", 0),
            "language": r.get("language"),
            "last_updated": r.get("pushed_at"),
        }
        for r in top_by_stars[:5]
    ]

    events = await gh.get_user_events(username, max_pages=EVENT_PAGES)
    commits_30d = commits_90d = prs_90d = 0
    oss = set()
    for event in events:
        created_str = event.get("created_at", "")
        if not created_str:
            continue
        age = (now - datetime.fromisoformat(created_str.replace("Z", "+00:00"))).days
        if event["type"] == "PushEvent":
            n = len(event.get("payload", {}).get("commits", []))
            if age <= 30:
                commits_30d += n
            if age <= 90:
                commits_90d += n
        if (event["type"] == "PullRequestEvent"
                and event.get("payload", {}).get("action") == "opened"
                and age <= 90):
            prs_90d += 1
        if event["type"] in ("PullRequestEvent", "PushEvent", "IssuesEvent",
                             "IssueCommentEvent"):
            repo_name = event.get("repo", {}).get("name", "")
            if repo_name and not repo_name.lower().startswith(f"{username.lower()}/"):
                oss.add(repo_name)

    readme = await gh.get_profile_readme(username)
    return {
        "name": user.get("name"),
        "bio": user.get("bio"),
        "followers": user.get("followers", 0),
        "public_repos": user.get("public_repos", 0),
        "account_age_days": age_days,
        "top_languages": list(breakdown.keys()),
        "language_breakdown": breakdown,
        "total_stars_received": sum(r.get("stargazers_count", 0) for r in repos),
        "total_forks_received": sum(r.get("forks_count", 0) for r in repos),
        "notable_repos": notable,
        "commits_last_30_days": commits_30d,
        "commits_last_90_days": commits_90d,
        "prs_opened_last_90_days": prs_90d,
        "major_oss_contributions": sorted(oss),
        "profile_readme_summary": readme[:500] if readme else None,
    }


async def stage1(usernames):
    done = load_done(SIGNALS_PATH)
    todo = [u for u in usernames if u not in done]
    print(f"Stage 1: fetching signals for {len(todo)} devs "
          f"({len(done)} already done)", flush=True)
    gh = GitHubClient()
    sem = asyncio.Semaphore(GH_CONCURRENCY)
    quota_lock = asyncio.Lock()
    # GraphQL is serialized with spacing: concurrent sustained calls trip
    # GitHub's secondary rate limit (403) even with primary quota to spare.
    gql_gate = asyncio.Lock()
    state = {"since_check": 0, "completed": 0, "failed": 0}

    async def fetch_minimal_contributions(gql, username):
        now = datetime.now(timezone.utc)
        resp = await gql.post(GRAPHQL_URL, json={
            "query": MINIMAL_CONTRIB_QUERY,
            "variables": {"login": username,
                          "from": (now - timedelta(days=180)).isoformat(),
                          "to": now.isoformat()},
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise ValueError(f"GraphQL minimal: {body['errors'][0].get('message')}")
        cc = body["data"]["user"]["contributionsCollection"]
        cc["commitContributionsByRepository"] = []
        # restrictedContributionsCount is the aggregation GitHub refuses to
        # compute for these users; show n/a rather than a false zero.
        cc["restrictedContributionsCount"] = "n/a"
        return cc

    async def contrib_throttled(gql, username):
        # Gate paces call *starts* only -- holding it through retry sleeps
        # would stall every other dev behind one failing query.
        async def spaced_fetch(months):
            async with gql_gate:
                await asyncio.sleep(GQL_SPACING)
            return await fetch_recent_contributions(gql, username, months=months)

        try:
            return await spaced_fetch(6), 6, False
        except ValueError as err:
            if "Resource limits" in str(err):
                try:
                    # Narrower labeled window beats losing org signals for
                    # exactly the top devs.
                    return await spaced_fetch(3), 3, False
                except ValueError:
                    pass
            async with gql_gate:
                await asyncio.sleep(GQL_SPACING)
            return await fetch_minimal_contributions(gql, username), 6, True

    async def ensure_quota():
        async with quota_lock:
            state["since_check"] += 1
            if state["since_check"] < QUOTA_CHECK_EVERY:
                return
            state["since_check"] = 0
            resp = await gh._get("/rate_limit")
            core = resp.json()["resources"]["core"]
            if core["remaining"] < QUOTA_FLOOR:
                wait = max(0.0, core["reset"] - time.time()) + 10
                print(f"PROGRESS S1 quota low ({core['remaining']}), "
                      f"sleeping {int(wait)}s until reset", flush=True)
                await asyncio.sleep(wait)

    async def one(gql, username):
        async with sem:
            await ensure_quota()
            started = time.time()
            try:
                profile = await fetch_profile_trimmed(gh, username)
                contrib, months, degraded = await contrib_throttled(gql, username)
                if degraded:
                    # Per-repo GraphQL data unavailable; recover org-repo star
                    # counts via REST for the events-derived contribution list.
                    enriched = []
                    for name in profile["major_oss_contributions"][:5]:
                        owner_repo = name.split("/", 1)
                        try:
                            info = await gh.get_repo_info(*owner_repo)
                            enriched.append(f"{name} ({info.get('stargazers_count', 0)} stars)")
                        except Exception:
                            enriched.append(name)
                    profile["major_oss_contributions"] = enriched
                row = {"username": username, "ok": True,
                       "signals": build_signals(username, profile, contrib,
                                                contrib_months=months),
                       "contrib_window_months": months,
                       "contrib_degraded": degraded,
                       "seconds": round(time.time() - started, 1)}
            except Exception as exc:
                state["failed"] += 1
                row = {"username": username, "ok": False,
                       "error": f"{type(exc).__name__}: {exc}",
                       "seconds": round(time.time() - started, 1)}
            append_row(SIGNALS_PATH, row)
            state["completed"] += 1
            if state["completed"] % 25 == 0:
                print(f"PROGRESS S1 {state['completed']}/{len(todo)} "
                      f"fail={state['failed']}", flush=True)
            return row

    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {gh._token}"}, timeout=30.0,
        ) as gql:
            results = await asyncio.gather(*(one(gql, u) for u in todo))
            failures = [r["username"] for r in results if not r["ok"]]
            if failures:
                print(f"PROGRESS S1 retry sweep: {len(failures)} failures", flush=True)
                state["failed"] = 0
                await asyncio.gather(*(one(gql, u) for u in failures))
    finally:
        await gh.close()


async def stage2(usernames):
    signals = load_done(SIGNALS_PATH)
    done = load_done(RESULTS_PATH)
    todo = [u for u in usernames if u in signals and u not in done
            and u not in PROVIDER_UNSCOREABLE]
    missing = [u for u in usernames if u not in signals]
    print(f"Stage 2: scoring {len(todo)} devs ({len(done)} already done, "
          f"{len(missing)} lack signals)", flush=True)
    client = AsyncOpenAI(api_key=os.environ["MOONSHOT_API_KEY"],
                         base_url=BASE_URL, timeout=300.0)
    sem = asyncio.Semaphore(K3_CONCURRENCY)
    state = {"completed": 0, "failed": 0, "tin": 0, "tout": 0}

    async def one(username):
        async with sem:
            started = time.time()
            try:
                evaluation, usages = await evaluate(client, signals[username]["signals"])
                row = {
                    "username": username, "ok": True, "eval": evaluation,
                    "signals": signals[username]["signals"],
                    "started_at": round(started, 3),
                    "completed_at": round(time.time(), 3),
                    "tokens": {"input": sum(u.prompt_tokens for u in usages),
                               "output": sum(u.completion_tokens for u in usages)},
                    "retries": len(usages) - 1,
                    "seconds": round(time.time() - started, 1),
                }
                state["tin"] += row["tokens"]["input"]
                state["tout"] += row["tokens"]["output"]
            except Exception as exc:
                state["failed"] += 1
                row = {"username": username, "ok": False,
                       "error": f"{type(exc).__name__}: {exc}",
                       "seconds": round(time.time() - started, 1)}
            append_row(RESULTS_PATH, row)
            state["completed"] += 1
            if state["completed"] % 25 == 0:
                cost = (state["tin"] / 1e6 * PRICE_INPUT_PER_M
                        + state["tout"] / 1e6 * PRICE_OUTPUT_PER_M)
                print(f"PROGRESS S2 {state['completed']}/{len(todo)} "
                      f"fail={state['failed']} cost=${cost:.2f}", flush=True)
            return row

    results = await asyncio.gather(*(one(u) for u in todo))
    failures = [r["username"] for r in results if not r["ok"]]
    if failures:
        print(f"PROGRESS S2 retry sweep: {len(failures)} failures", flush=True)
        await asyncio.gather(*(one(u) for u in failures))


def summarize(usernames, s1_wall, s2_wall):
    rows = load_done(RESULTS_PATH)
    scored = [rows[u] for u in usernames if u in rows]
    ranked = sorted(scored, key=lambda r: r["eval"]["score"], reverse=True)

    with LEADERBOARD_PATH.open("w") as fh:
        for i, row in enumerate(ranked, 1):
            ev = row["eval"]
            flag = f"  [flag: {ev['flag']}]" if ev.get("flag") else ""
            fh.write(f"{i:>4}. {ev['score']:>3}  {row['username']:<30} "
                     f"{ev['one_liner']}{flag}\n")

    print("\n" + "=" * 70 + "\nFULL RUN SUMMARY\n" + "=" * 70)
    print(f"Scored: {len(scored)}/{len(usernames)}")
    print("\nBand histogram:")
    for lo, hi in BANDS:
        n = sum(1 for r in scored if lo <= r["eval"]["score"] <= hi)
        print(f"  {lo:>3}-{hi:<3}: {n:>4}  {'#' * (n // 10)}")
    retries = sum(r.get("retries", 0) for r in scored)
    tin = sum(r["tokens"]["input"] for r in scored)
    tout = sum(r["tokens"]["output"] for r in scored)
    cost = tin / 1e6 * PRICE_INPUT_PER_M + tout / 1e6 * PRICE_OUTPUT_PER_M
    print(f"\nJSON retries: {retries} across {len(scored)} devs")
    print(f"Tokens: {tin:,} in / {tout:,} out")
    print(f"Cost: ${cost:.2f} (uncached-rate estimate)")
    print(f"Wall time: stage 1 (GitHub fetch) {s1_wall}s, "
          f"stage 2 (K3 inference) {s2_wall}s")
    print(f"Leaderboard: {LEADERBOARD_PATH}")


async def main():
    cohort = json.loads(COHORT_PATH.read_text())["cohort"]
    usernames = [c["login"] for c in cohort]
    t0 = time.time()
    await stage1(usernames)
    t1 = time.time()
    await stage2(usernames)
    t2 = time.time()
    summarize(usernames, round(t1 - t0, 1), round(t2 - t1, 1))


if __name__ == "__main__":
    asyncio.run(main())
