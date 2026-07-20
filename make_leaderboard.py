#!/usr/bin/env python3
"""Generate leaderboard.html from the run results. No model calls.

Reads private-data/scout_results_full.jsonl + signals metadata + cohort.json,
embeds the rows as JSON, and renders the static page. Refuses to build if any
private validation account leaks into the page data.
"""

import json
import statistics
from pathlib import Path

RESULTS = Path("private-data/signals_full.jsonl")
SCORES = Path("private-data/scout_results_full.jsonl")
COHORT = Path("cohort.json")
ROSTER = Path("private-data/pilot_roster.json")
OUT = Path("leaderboard.html")
# GitHub Pages copy: the public release serves docs/ so the site lives at the
# root URL without exposing anything else in the branch.
PAGES_OUT = Path("docs/index.html")
TEMPLATE = Path("leaderboard_template.html")

BANDS = [(90, 100, "90-100"), (70, 89, "70-89"), (50, 69, "50-69"),
         (30, 49, "30-49"), (1, 29, "1-29")]


def band_of(score):
    for lo, hi, name in BANDS:
        if lo <= score <= hi:
            return name
    raise ValueError(score)


def main():
    cohort = {c["login"]: c for c in json.loads(COHORT.read_text())["cohort"]}
    rank_in_cohort = {u: i for i, u in enumerate(cohort)}

    meta = {}
    for line in RESULTS.read_text().splitlines():
        row = json.loads(line)
        if row.get("ok"):
            meta[row["username"]] = row

    scored = {}
    for line in SCORES.read_text().splitlines():
        row = json.loads(line)
        if row.get("ok") and row["username"] in cohort:
            scored[row["username"]] = row

    # The five pilot sparse-tier accounts are private validation data and
    # must never appear in a public artifact.
    private = set(json.loads(ROSTER.read_text())["developers"][-5:])
    leaked = private & (set(scored) | set(cohort))
    if leaked:
        raise SystemExit(f"refusing to build: private accounts in page data: {leaked}")

    ranked = sorted(scored.values(),
                    key=lambda r: (-r["eval"]["score"], rank_in_cohort[r["username"]]))
    rows = []
    for i, r in enumerate(ranked, 1):
        u, ev = r["username"], r["eval"]
        m, c = meta.get(u, {}), cohort[u]
        rows.append({
            "r": i, "s": ev["score"], "u": u, "b": band_of(ev["score"]),
            "o": ev["one_liner"], "f": ev.get("flag"),
            "fw": c["followers"], "ec": c["ecosystem_commits"],
            "sr": c["seed_repos_contributed"], "ad": c["account_age_days"],
            "w3": m.get("contrib_window_months") == 3,
            "dg": bool(m.get("contrib_degraded")),
        })

    scores = [x["s"] for x in rows]
    band_counts = {name: sum(1 for x in rows if x["b"] == name) for _, _, name in BANDS}
    # The 1-29 band is aggregated, not individually named: sparse-window
    # accounts can't be ranked meaningfully, and naming them would
    # misrepresent contributors whose ecosystem work predates the window.
    named = [x for x in rows if x["s"] >= 30]
    stats = {
        "scored": len(rows),
        "cohort": 1000,
        "median": int(statistics.median(scores)),
        "top": max(scores),
        "cost": "17.09",
        "w3": sum(1 for x in rows if x["w3"]),
        "dg": sum(1 for x in rows if x["dg"]),
        "flagged": sum(1 for x in rows if x["f"]),
        "bands": band_counts,
        "listed": len(named),
        "agg": {"band": "1-29", "count": len(rows) - len(named)},
        "generated": "July 19, 2026",
    }

    html = (TEMPLATE.read_text()
            .replace("__DATA__", json.dumps(named, ensure_ascii=False))
            .replace("__STATS__", json.dumps(stats))
            .replace("__COST__", stats["cost"])
            .replace("__GENERATED__", stats["generated"]))
    OUT.write_text(html)
    PAGES_OUT.parent.mkdir(exist_ok=True)
    PAGES_OUT.write_text(html)
    print(f"wrote {OUT} + {PAGES_OUT} ({OUT.stat().st_size // 1024} KB, "
          f"{len(named)} named rows, {stats['agg']['count']} aggregated)")


if __name__ == "__main__":
    main()
