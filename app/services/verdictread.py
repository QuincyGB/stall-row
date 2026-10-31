"""Public Verdict Read + Embeddable Verdict Widget (Next-20 #13).

GET /v1/verdict-read?url= is a FREE read over state this service
ALREADY HOLDS from checks someone funded — never a fresh probe.
This module imports no HTTP client and makes no outbound call of
any kind: the answer is composed entirely from local stores, and a
target with no recorded state answers found=false. The target URL
is a lookup key only — it is NEVER fetched (no SSRF surface: there
is no fetch to guard).

What recorded verdict material actually exists (checked 2026-10-31):

  * Guard verdicts are NOT persisted. services/guard.py composes
    its PROCEED / CAUTION / ABORT live from a fresh unpaid sign
    probe on every paid call and writes nothing to any store, so
    no recorded Guard verdict exists to serve.
  * RouteVerdict verdicts from STANDING WATCHES are recorded: the
    route_verdict watch leg (services/watch.py) stores each run's
    summary {verdict, score, amount, pay_to, network} on its watch
    registration (watch_regs.last_summary, with last_run_at /
    last_status). When one exists for the URL it is served
    verbatim, labelled verdict_source="recorded route-verdict
    check".
  * Recheck history (services/recheck.py) records every completed
    funded check: status (BASELINE / UNCHANGED / CHANGED), the
    full fingerprint (status_code, amount, asset, network, payTo),
    and checked_at — the Score Book's aggregates are computed over
    exactly these records.
  * The Bad-payTo Registry screen (services/registry.py) is itself
    a read over local state (the bundled OFAC snapshot + the Index
    snapshot pair), so the latest recorded payTo can be screened
    without touching the target.

Verdict derivation, stated in every response via verdict_source:

  1. "recorded route-verdict check" — a standing watch recorded a
     RouteVerdict verdict for this URL; served verbatim with its
     score and the run's time.
  2. "derived from recorded checks" — no recorded RouteVerdict
     verdict, but recorded Recheck checks carry payment terms.
     The band mirrors Guard's recorded-data legs only (Guard's
     live sign-parse leg cannot be replayed from a fingerprint):
     a flagged recorded payTo is ABORT; a payTo that moved across
     recorded checks, or a latest recorded sign that did not
     present a 402, is CAUTION; stable recorded terms with an
     unflagged payTo are PROCEED. Every reason is listed.
  3. null — no verdict is honestly derivable (no recorded checks
     with payment terms, e.g. a web-page target or a catalogue /
     patrol-only target). verdict is null too: "not yet checked"
     is a state, not a verdict, and none is fabricated.

last_change_at is the newest recorded CHANGED event for the URL
across both recorded sources (a Recheck history record with
status CHANGED, or a route_verdict watch whose last run reported
CHANGED, at its last_run_at).

The embed (GET /widget/verdict.js and GET /widget/verdict.html)
renders exactly this read — verdict + last-change — so an
inspector or directory can drop it into a page. Branding is the
fleet's published identity: "Verdict Row Patrol",
patrol@verdictrow.com. No owner name appears anywhere.
"""

from __future__ import annotations

import html
import time
from urllib.parse import urlsplit

from . import registry as registry_svc
from . import scorebook as scorebook_svc

BRAND = "Verdict Row Patrol"
CONTACT = "patrol@verdictrow.com"

LIMITS = (
    "This read serves only state this service already holds from "
    "checks someone funded: the Recheck history, standing-watch "
    "RouteVerdict summaries, Score Book aggregates, the catalogue "
    "snapshots held, the patrol snapshot, and the Bad-payTo "
    "Registry's local sources. It never probes, fetches, or pays "
    "the target, and it never triggers a paid call. Absence here "
    "means 'not in our records', never 'does not exist'. A verdict "
    "labelled 'derived from recorded checks' is computed from "
    "recorded terms and history only — it is not a live Guard call "
    "(Guard verdicts are not persisted; buy POST /v1/guard for a "
    "live one)."
)

# The registry feed is assembled from local stores, but rebuilding
# it walks the whole OFAC snapshot + the Index snapshot diff; cache
# the pay_to -> entry map briefly so the public read stays cheap.
_FLAGGED_TTL_S = 60.0
_flagged_cache: dict = {"key": None, "at": 0.0, "map": {}}


def _flagged_map(index_store) -> dict:
    now = time.monotonic()
    key = id(index_store)
    if (
        _flagged_cache["key"] == key
        and now - _flagged_cache["at"] < _FLAGGED_TTL_S
    ):
        return _flagged_cache["map"]
    feed = registry_svc.build_feed(index_store)
    mapping = {}
    for entry in feed.get("entries") or []:
        pay_to = entry.get("pay_to")
        if isinstance(pay_to, str):
            mapping[pay_to.lower()] = entry
    _flagged_cache.update({"key": key, "at": now, "map": mapping})
    return mapping


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    return url.rstrip("/")


def _rv_summaries(store, url: str) -> list[dict]:
    """Recorded standing-watch RouteVerdict summaries for the URL
    (the store accessor reads watch_regs only — local state)."""
    try:
        rows = store.route_verdict_summaries_for_url(url)
    except AttributeError:
        return []
    return rows or []


def verdict_read(store, index_store, url: str, patrol_snapshot="default") -> dict:
    """The recorded verdict read for one resource URL. Pure local
    store reads: no client, no fetch, no payment machinery."""
    url = _validate_url(url)
    book = scorebook_svc.score_book(
        store, index_store, url=url, patrol_snapshot=patrol_snapshot
    )
    live = book.get("live_checks") or {}
    records = [
        r
        for r in scorebook_svc._records(store)
        if (r.get("url") or "").rstrip("/") == url
    ]
    latest = records[-1] if records else None
    rv_rows = _rv_summaries(store, url)
    rv_latest = rv_rows[-1] if rv_rows else None

    # --- last recorded CHANGED event, across both recorded sources ---
    change_times = [
        r["checked_at"]
        for r in records
        if r.get("status") == "CHANGED" and r.get("checked_at")
    ]
    for row in rv_rows:
        if row.get("last_status") == "CHANGED" and row.get("last_run_at"):
            change_times.append(row["last_run_at"])
    last_change_at = max(change_times) if change_times else None

    # --- recorded terms: latest check fingerprint, then the
    # recorded RouteVerdict summary, then the catalogue entry ---
    terms = None
    terms_source = None
    fingerprint = (latest or {}).get("current") or None
    if fingerprint:
        terms = {
            "status_code": fingerprint.get("status_code"),
            "amount": fingerprint.get("amount"),
            "asset": fingerprint.get("asset"),
            "network": fingerprint.get("network"),
            "payTo": fingerprint.get("payTo"),
        }
        terms_source = "latest recorded check"
    elif rv_latest and rv_latest.get("summary"):
        summary = rv_latest["summary"]
        terms = {
            "status_code": None,
            "amount": summary.get("amount"),
            "asset": None,
            "network": summary.get("network"),
            "payTo": summary.get("pay_to"),
        }
        terms_source = "recorded route-verdict check"
    else:
        entry = (book.get("catalogue") or {}).get("latest_entry")
        if entry:
            terms = {
                "status_code": None,
                "amount": entry.get("amount"),
                "asset": entry.get("asset"),
                "network": entry.get("network"),
                "payTo": entry.get("payTo"),
            }
            terms_source = "latest catalogue snapshot entry"

    # --- registry screen over the recorded payTo (local sources) ---
    payto_flag = None
    pay_to = (terms or {}).get("payTo")
    if pay_to:
        payto_flag = _flagged_map(index_store).get(str(pay_to).lower())

    # --- verdict: recorded first, honestly derived second ---
    verdict = None
    verdict_source = None
    verdict_at = None
    verdict_reasons: list[dict] = []
    if rv_latest and (rv_latest.get("summary") or {}).get("verdict"):
        summary = rv_latest["summary"]
        verdict = summary["verdict"]
        verdict_source = "recorded route-verdict check"
        verdict_at = rv_latest.get("last_run_at")
        verdict_reasons = [
            {
                "code": "recorded_route_verdict",
                "detail": (
                    "a standing watch recorded this RouteVerdict "
                    f"verdict (score {summary.get('score')}) at "
                    f"{verdict_at}; served verbatim, not recomputed"
                ),
            }
        ]
        if payto_flag is not None:
            verdict_reasons.append(
                {
                    "code": "payto_flagged_since",
                    "detail": (
                        "the currently recorded payTo is in the "
                        "bad-payTo registry: "
                        + ", ".join(payto_flag.get("reasons") or [])
                        + " — this post-dates or sits outside the "
                        "recorded verdict above; read both"
                    ),
                }
            )
    elif fingerprint and fingerprint.get("payTo"):
        verdict_source = "derived from recorded checks"
        verdict_at = latest.get("checked_at")
        caution = False
        if payto_flag is not None:
            verdict = "ABORT"
            verdict_reasons.append(
                {
                    "code": "payto_flagged",
                    "detail": (
                        "recorded payTo is in the bad-payTo registry: "
                        + ", ".join(payto_flag.get("reasons") or [])
                    ),
                }
            )
        else:
            verdict_reasons.append(
                {
                    "code": "payto_not_flagged",
                    "detail": "recorded payTo absent from the bad-payTo registry's sources",
                }
            )
            if (live.get("payto_changes") or 0) > 0:
                caution = True
                verdict_reasons.append(
                    {
                        "code": "payto_moved_before",
                        "detail": (
                            f"this resource has carried {live['payto_changes'] + 1} "
                            "distinct payTos across recorded checks"
                        ),
                    }
                )
            if (live.get("price_changes") or 0) > 0:
                verdict_reasons.append(
                    {
                        "code": "price_moved_before",
                        "detail": (
                            f"price changed {live['price_changes']} time(s) across "
                            f"{live.get('checks', 0)} recorded checks"
                        ),
                    }
                )
            status_code = fingerprint.get("status_code")
            if status_code is not None and status_code != 402:
                caution = True
                verdict_reasons.append(
                    {
                        "code": "recorded_sign_not_402",
                        "detail": (
                            f"the latest recorded check saw HTTP "
                            f"{status_code}, not a 402 challenge"
                        ),
                    }
                )
            verdict = "CAUTION" if caution else "PROCEED"
            if verdict == "PROCEED":
                verdict_reasons.append(
                    {
                        "code": "recorded_terms_stable",
                        "detail": (
                            "recorded terms stable across "
                            f"{live.get('checks', 0)} recorded check(s), "
                            "recorded payTo unflagged, latest recorded "
                            "sign presented a 402"
                        ),
                    }
                )

    found = bool(book.get("found")) or bool(rv_rows)
    return {
        "url": url,
        "found": found,
        "read_at": scorebook_svc._now_iso(),
        "verdict": verdict,
        "verdict_source": verdict_source,
        "verdict_at": verdict_at,
        "verdict_reasons": verdict_reasons,
        "route_verdict": (
            {
                "verdict": (rv_latest.get("summary") or {}).get("verdict"),
                "score": (rv_latest.get("summary") or {}).get("score"),
                "checked_at": rv_latest.get("last_run_at"),
                "watch_status": rv_latest.get("last_status"),
            }
            if rv_latest
            else None
        ),
        "latest_check": (
            {
                "checked_at": latest.get("checked_at"),
                "status": latest.get("status"),
                "kind": latest.get("kind"),
                "summary": latest.get("summary"),
            }
            if latest
            else None
        ),
        "last_change_at": last_change_at,
        "terms": terms,
        "terms_source": terms_source,
        "payto_flag": payto_flag,
        "score_book": {
            "checks": live.get("checks", 0),
            "first_checked_at": live.get("first_checked_at"),
            "last_checked_at": live.get("last_checked_at"),
            "by_status": live.get("by_status", {}),
            "changed_count": live.get("changed_count", 0),
            "amounts_seen": live.get("amounts_seen", []),
            "pay_tos_seen": live.get("pay_tos_seen", []),
            "networks_seen": live.get("networks_seen", []),
            "price_changes": live.get("price_changes", 0),
            "payto_changes": live.get("payto_changes", 0),
        },
        "catalogue": {
            "present_in_snapshots": (book.get("catalogue") or {}).get(
                "present_in_snapshots", 0
            ),
            "in_latest_snapshot": (book.get("catalogue") or {}).get(
                "in_latest_snapshot", False
            ),
            "last_snapshot_seen_at": (book.get("catalogue") or {}).get(
                "last_snapshot_seen_at"
            ),
        },
        "patrol": {
            "open_defect_count": (book.get("patrol") or {}).get(
                "open_defect_count", 0
            ),
            "recovery_count": (book.get("patrol") or {}).get(
                "recovery_count", 0
            ),
        },
        "limits": LIMITS,
    }


# --- the embed -----------------------------------------------------------------

WIDGET_JS = """/* Verdict Row Patrol — embeddable verdict widget.
   Usage:
     <script src="https://stall-row-production.up.railway.app/widget/verdict.js"
             data-target="https://example.com/v1/your-route" async></script>
   The script renders the recorded verdict for data-target's URL
   into a shadow-DOM element placed right after the script tag.
   Data comes from the free GET /v1/verdict-read surface, which
   serves recorded state only — no probe, no paid call per view. */
(function () {
  var script = document.currentScript;
  if (!script) return;
  var target = script.getAttribute("data-target");
  if (!target) return;
  var host = document.createElement("div");
  host.className = "vr-verdict-widget";
  script.parentNode.insertBefore(host, script.nextSibling);
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
  root.innerHTML =
    "<style>" +
    ".card{font:14px/1.45 system-ui,sans-serif;border:1px solid #d8d8d8;" +
    "border-radius:10px;padding:10px 12px;max-width:340px;color:#1a1a1a;background:#fff}" +
    ".brand{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#666}" +
    ".verdict{font-size:20px;font-weight:700;margin:2px 0}" +
    ".meta{font-size:12px;color:#444}.meta div{margin:1px 0}" +
    "a{color:#0b5cad}</style>" +
    '<div class="card"><div class="brand">Verdict Row Patrol</div>' +
    '<div class="verdict">Checking\\u2026</div><div class="meta"></div></div>';
  function setText(sel, text) {
    var el = root.querySelector(sel);
    if (el) el.textContent = text;
  }
  function line(label, value) {
    var div = document.createElement("div");
    div.textContent = label + ": " + value;
    return div;
  }
  var readUrl = new URL("/v1/verdict-read", script.src);
  readUrl.searchParams.set("url", target);
  fetch(readUrl.toString(), { headers: { Accept: "application/json" } })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var meta = root.querySelector(".meta");
      meta.textContent = "";
      if (!d.found) {
        setText(".verdict", "Not yet checked");
        meta.appendChild(line("Target", target));
      } else {
        setText(".verdict", d.verdict ? "Verdict: " + d.verdict : "No verdict recorded");
        if (d.verdict_source) meta.appendChild(line("Source", d.verdict_source));
        if (d.verdict_at) meta.appendChild(line("Verdict at", d.verdict_at));
        meta.appendChild(line("Last change", d.last_change_at || "none recorded"));
        if (d.latest_check) meta.appendChild(line("Last check", d.latest_check.checked_at || "unknown"));
      }
      var foot = document.createElement("div");
      var a = document.createElement("a");
      a.href = "mailto:patrol@verdictrow.com";
      a.textContent = "patrol@verdictrow.com";
      foot.appendChild(document.createTextNode("Verdict Row Patrol \\u00b7 "));
      foot.appendChild(a);
      meta.appendChild(foot);
    })
    .catch(function () { setText(".verdict", "Verdict unavailable"); });
})();
"""


def render_widget_html(read: dict | None, url: str) -> str:
    """The self-contained iframe page: server-rendered from the
    recorded read (no client JS needed), inline styles only, every
    interpolated value HTML-escaped — the url parameter in
    particular is attacker-controlled."""
    esc = html.escape
    target = esc(url or "", quote=True)
    if read is None:
        body = (
            '<div class="verdict">Invalid target</div>'
            f'<div class="meta">Target: {target}</div>'
        )
    elif not read.get("found"):
        body = (
            '<div class="verdict">Not yet checked</div>'
            f'<div class="meta">Target: {target}<br>'
            "No recorded verdict in Verdict Row Patrol's records "
            "for this URL yet.</div>"
        )
    else:
        verdict = read.get("verdict")
        lines = [f"Target: {target}"]
        if read.get("verdict_source"):
            lines.append(f"Source: {esc(str(read['verdict_source']))}")
        if read.get("verdict_at"):
            lines.append(f"Verdict at: {esc(str(read['verdict_at']))}")
        lines.append(
            "Last change: "
            + esc(str(read.get("last_change_at") or "none recorded"))
        )
        latest = read.get("latest_check") or {}
        if latest.get("checked_at"):
            lines.append(f"Last check: {esc(str(latest['checked_at']))}")
        terms = read.get("terms") or {}
        if terms.get("amount") is not None:
            lines.append(
                "Recorded terms: amount "
                + esc(str(terms["amount"]))
                + " on "
                + esc(str(terms.get("network") or "unknown network"))
            )
        body = (
            '<div class="verdict">'
            + esc(f"Verdict: {verdict}" if verdict else "No verdict recorded")
            + "</div>"
            + '<div class="meta">'
            + "<br>".join(lines)
            + "</div>"
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Verdict Row Patrol — verdict widget</title>"
        "<style>"
        "body{margin:0;font:14px/1.45 system-ui,sans-serif;color:#1a1a1a}"
        ".card{border:1px solid #d8d8d8;border-radius:10px;"
        "padding:10px 12px;max-width:340px;background:#fff}"
        ".brand{font-size:11px;letter-spacing:.04em;"
        "text-transform:uppercase;color:#666}"
        ".verdict{font-size:20px;font-weight:700;margin:2px 0}"
        ".meta{font-size:12px;color:#444}"
        "a{color:#0b5cad}"
        "</style></head><body>"
        '<div class="card">'
        f'<div class="brand">{BRAND}</div>'
        f"{body}"
        '<div class="meta">Verdict Row Patrol · '
        f'<a href="mailto:{CONTACT}">{CONTACT}</a></div>'
        "</div></body></html>\n"
    )
