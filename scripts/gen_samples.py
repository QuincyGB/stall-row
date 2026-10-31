#!/usr/bin/env python3
"""Fleet sample-data generator (Batch 2 #9) — Ren, Verdict Row Patrol.

Builds the committed free sample responses served at GET /samples and
GET /sample/<route-slug> on every Verdict Row stall. One sample per
priced route (40 across the fleet).

Provenance rules (the integrity core):
- "recorded" samples are REAL recorded responses of the route against a
  real target, pulled from the service's own public recorded-state
  surfaces (StallRow's Recheck feed; RouteVerdict's /v1/history read).
  Nothing else may carry the recorded label.
- Every other route gets a "schema-example": the route's own discovery
  output_example (the shipped example existing tests validate against
  the route's outputSchema), with real fleet subjects, recorded_at=null
  and an explicit sample_note saying it is a shape example, not a
  recorded result. No sample is ever synthesized from a sibling route.

Output per repo: app/data/samples/<slug>.json + app/data/samples-manifest.json
Run: ~/workspace/.fleet-venv/bin/python scripts/gen_samples.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

WS = Path.home() / "workspace"
GENERATED_BY = "Ren, Verdict Row Patrol — patrol@verdictrow.com"
SCHEMA_NOTE = (
    "Shape example built from this route's documented output example "
    "(validated against its outputSchema) with real fleet subjects — "
    "not a recorded result. Buy the route for live data."
)
RECORDED_NOTE = (
    "Recorded response of this route against the target below, served "
    "from the fleet's recorded state — not live data."
)
FLEET_TARGET = "https://token-verdict-production.up.railway.app/v1/verdict"

# Per-repo route -> (example attr, schema attr, input-example attr) for the
# single-purpose stalls; StallRow is read from its ROUTES table instead.
SIBLING_ROUTES = {
    "token-verdict": {
        "/v1/verdict": ("VERDICT_OUTPUT_EXAMPLE", "VERDICT_OUTPUT_SCHEMA", "VERDICT_INPUT_EXAMPLE"),
        "/v1/pre-trade": ("PRE_TRADE_OUTPUT_EXAMPLE", "PRE_TRADE_OUTPUT_SCHEMA", "PRE_TRADE_INPUT_EXAMPLE"),
        "/v1/batch-verdict": ("BATCH_OUTPUT_EXAMPLE", "BATCH_OUTPUT_SCHEMA", "BATCH_INPUT_EXAMPLE"),
    },
    "route-verdict": {
        "/v1/check": ("CHECK_OUTPUT_EXAMPLE", "CHECK_OUTPUT_SCHEMA", "CHECK_INPUT_EXAMPLE"),
    },
    "list-smith": {
        "/v1/grade": ("GRADE_OUTPUT_EXAMPLE", "GRADE_OUTPUT_SCHEMA", "GRADE_INPUT_EXAMPLE"),
        "/v1/fix": ("FIX_OUTPUT_EXAMPLE", "FIX_OUTPUT_SCHEMA", "FIX_INPUT_EXAMPLE"),
    },
    "lock-smith": {
        "/v1/audit": ("AUDIT_OUTPUT_EXAMPLE", "AUDIT_OUTPUT_SCHEMA", "AUDIT_INPUT_EXAMPLE"),
    },
}

EXTRACT_SNIPPET = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
repo = sys.argv[2]
out = {}
if repo == "stall-row":
    from app.discovery import ROUTES
    for path, r in ROUTES.items():
        out[path] = {
            "example": r.get("output_example"),
            "input_example": r.get("input_example"),
            "schema_props": sorted((r.get("output_schema") or {}).get("properties") or {}),
        }
else:
    import app.discovery as d
    spec = json.loads(sys.argv[3])
    for path, (ex, sc, inp) in spec.items():
        schema = getattr(d, sc)
        out[path] = {
            "example": getattr(d, ex),
            "input_example": getattr(d, inp),
            "schema_props": sorted((schema or {}).get("properties") or {}),
        }
print(json.dumps(out, default=str))
"""


def fetch_jsonl_first(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        line = resp.readline().decode()
    return json.loads(line)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def slug_of(route: str) -> str:
    return route.removeprefix("/v1/").strip("/").replace("/", "-")


def target_of(route: str, input_example, body) -> str:
    if isinstance(body, dict):
        for key in ("resource_url", "url", "endpoint_url", "target"):
            if isinstance(body.get(key), str):
                return body[key]
    if isinstance(input_example, dict):
        for key in ("resource_url", "url", "endpoint_url", "address", "token_address", "tx_hash", "repo", "query"):
            if isinstance(input_example.get(key), str):
                return input_example[key]
        if isinstance(input_example.get("token_addresses"), list) and input_example["token_addresses"]:
            return ", ".join(input_example["token_addresses"])
    return f"POST {route} on this stall (fleet's own service as subject)"


def retarget(obj, old: str, new: str):
    if isinstance(obj, dict):
        return {k: retarget(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [retarget(v, old, new) for v in obj]
    if isinstance(obj, str):
        return obj.replace(old, new)
    return obj


def main() -> None:
    # --- recorded bodies from the services' own recorded-state surfaces ---
    recheck_rec = fetch_jsonl_first(
        "https://stall-row-production.up.railway.app/v1/recheck/feed"
    )
    rv_hist = fetch_json(
        "https://route-verdict-production.up.railway.app/v1/history"
        "?resource_url=https%3A%2F%2Ftoken-verdict-production.up.railway.app%2Fv1%2Fverdict"
    )
    rv_rec = rv_hist["checks"][0]
    recorded = {
        ("stall-row", "/v1/recheck"): (
            recheck_rec,
            recheck_rec["url"],
            recheck_rec["checked_at"],
            "GET https://stall-row-production.up.railway.app/v1/recheck/feed (newest record)",
        ),
        ("route-verdict", "/v1/check"): (
            rv_rec,
            rv_rec["resource_url"],
            rv_rec["checked_at"],
            "GET https://route-verdict-production.up.railway.app/v1/history?resource_url=<fleet stall route> (latest check)",
        ),
    }

    py = str(Path.home() / "workspace" / ".fleet-venv" / "bin" / "python")
    for repo in ["stall-row", "token-verdict", "route-verdict", "list-smith", "lock-smith"]:
        spec = SIBLING_ROUTES.get(repo, {})
        proc = subprocess.run(
            [py, "-c", EXTRACT_SNIPPET, str(WS / repo), repo, json.dumps(spec)],
            capture_output=True, text=True, check=True,
        )
        routes = json.loads(proc.stdout)
        samples_dir = WS / repo / "app" / "data" / "samples"
        samples_dir.mkdir(parents=True, exist_ok=True)
        manifest_samples = []
        for route in sorted(routes):
            info = routes[route]
            body = info["example"]
            assert isinstance(body, dict), f"{repo} {route}: example is not an object"
            if repo == "list-smith":
                body = retarget(body, "https://target.example.com/v1/thing", FLEET_TARGET)
            slug = slug_of(route)
            key = (repo, route)
            if key in recorded:
                rbody, target, recorded_at, pulled_from = recorded[key]
                wrapper = {
                    "sample": True, "live": False, "source": "recorded",
                    "recorded_at": recorded_at, "target": target, "route": route,
                    "sample_note": RECORDED_NOTE,
                }
                sample = {**rbody, **wrapper}
                entry = {"route": route, "slug": slug, "source": "recorded",
                         "target": target, "recorded_at": recorded_at,
                         "pulled_from": pulled_from}
            else:
                target = target_of(route, info.get("input_example"), body)
                if repo == "list-smith":
                    target = FLEET_TARGET
                wrapper = {
                    "sample": True, "live": False, "source": "schema-example",
                    "recorded_at": None, "target": target, "route": route,
                    "sample_note": SCHEMA_NOTE,
                }
                sample = {**body, **wrapper}
                entry = {"route": route, "slug": slug, "source": "schema-example",
                         "target": target, "recorded_at": None,
                         "pulled_from": "app/discovery.py output example for this route (schema-validated)"}
            # A route body may itself carry a marker-named field (e.g.
            # agent-card-verify's own "source"); the in-band sample
            # marker wins by spec, and the shadowed value is recorded.
            for mk in wrapper:
                if mk in body and body[mk] != wrapper[mk]:
                    entry[f"shadowed_body_field_{mk}"] = body[mk]
            for k in ("sample", "live", "source", "recorded_at"):
                assert sample[k] == {"sample": True, "live": False}.get(k, sample[k]) or k in ("source", "recorded_at"), k
            assert sample["sample"] is True and sample["live"] is False
            (samples_dir / f"{slug}.json").write_text(
                json.dumps(sample, indent=2, sort_keys=True) + "\n"
            )
            manifest_samples.append(entry)
            print(f"{repo} {route} -> {slug}.json [{entry['source']}] props={len(info['schema_props'])}")
        manifest = {
            "kind": "verdict-row-samples/v1",
            "service": repo,
            "generated_by": GENERATED_BY,
            "generator": "stall-row/scripts/gen_samples.py",
            "note": "Free sample responses, one per priced route. source=recorded: a real recorded response from the fleet's recorded state. source=schema-example: shape example from the route's documented output example — not a recorded result. Every sample body carries sample=true + live=false in-band.",
            "samples": manifest_samples,
        }
        (WS / repo / "app" / "data" / "samples-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        print(f"{repo}: {len(manifest_samples)} samples")


if __name__ == "__main__":
    main()
