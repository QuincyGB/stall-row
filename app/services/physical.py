"""Inspector Physical: POST /v1/physical ($0.05) — the whole
pre-pay workup for ONE x402 resource in one call, one payment,
one response (Next-20 #9).

The four inspector singles a careful buyer runs before paying an
endpoint it does not know — Sign Reader ($0.01), Score Book
($0.01), Guard ($0.01), Recheck ($0.005), $0.075 bought separately
— composed here in-process at $0.05, the way Guard composes Sign
Reader + Score Book: no sibling HTTP calls, no new probing
machinery, no new scoring system.

Leg order is deliberate. Sign, Book, and Guard run FIRST, over the
records as they stood when the call arrived, so their sections
mean exactly what the singles mean — in particular Guard's
no-history caution is computed on history that predates this call.
The Recheck leg runs LAST, through the single route's own path
(services/recheck.py recheck(), index store included): it probes
the target and RECORDS the check, so the physical's recheck lands
in the feed/history exactly like a normal recheck — and, because
it runs last, its fresh record can't quietly retune the other
legs' answers (this codebase's no-silent-retune rule).

RouteVerdict is deliberately NOT a leg: including it would push
the bundle past the $0.05 auto-test ceiling the fleet's standing
checkers run under, and the physical's question is the endpoint's
sign, history, and pay/don't-pay verdict — not a route grade.

Summary derivation: the headline `verdict` IS Guard's verdict,
verbatim — Guard already composes the sign and the history, so
inventing a second scoring on top would only blur it. The
recheck's status (BASELINE / UNCHANGED / CHANGED) is stated
beside it, never folded into it.

Failure discipline (fleet law: never charge on a failed call):
  * A leg delivering a report is a DELIVERED section — an
    unreachable target (sign defect + Guard ABORT), an unparseable
    sign, a book with found:false. The physical exists exactly to
    report bad targets.
  * The recheck leg's probe failure is the one exception in shape,
    not in spirit: the single Recheck raises UpstreamError for a
    failed probe (its own contract: 502, nothing recorded, stored
    state untouched). Here that failure is rendered as the recheck
    section itself — status PROBE_FAILED, the error named,
    recorded:false — because the other three legs have real
    reports to deliver about the same dead target. Nothing is
    recorded and no baseline is touched on that path, exactly as
    the single behaves.
  * An SSRF-refused target raises ValueError out of the sign leg
    before anything else runs (the handler's 422; the paywall
    never settles on >= 400, and a Fleet Pass debit is refunded by
    the middleware on >= 400).
  * Any other exception from our own machinery propagates — the
    call fails cleanly and no payment settles.

Target shape: one resource URL (the union the four legs share —
Sign Reader, Guard, and Recheck all take a URL; the Book leg is
run for that URL). Score Book's origin / pay_to shapes are NOT
accepted here: a physical is an examination of one endpoint, and
an origin or payTo has no sign to read and nothing to guard.
Anything that is not a non-empty URL string is refused (422)
rather than half-served.
"""

from __future__ import annotations

import logging

import httpx

from ..rpc import UpstreamError
from . import guard as guard_svc
from . import recheck as recheck_svc
from . import scorebook as scorebook_svc
from . import signread as signread_svc

log = logging.getLogger("stall-row.physical")

PRICE_UNITS = 50_000  # $0.05 — mirrors config.PRICE_UNITS

LIMITS = (
    "One call, four legs, at call time: the sign is read live "
    "(one unpaid probe set, the sign reader's discipline), the "
    "book and the guard's history are computed over the records "
    "held when the call arrived, and the recheck leg probes and "
    "records after them — so this call's own recheck never lifts "
    "the guard's no-history caution; the NEXT call's book will "
    "include it. RouteVerdict is not part of the physical. The "
    "legs' own limits apply section by section."
)


async def physical(
    client: httpx.AsyncClient,
    store,
    index_store,
    url: str,
    watch_id: str | None = None,
) -> dict:
    """Run the four legs in-process on one target URL. Raises
    ValueError only when the target is not a servable URL or the
    sign leg's SSRF guard refuses it (the handler's 422)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("name one target: a resource url")
    url = url.strip()

    # --- sign leg (full Sign Reader report) -----------------------
    # Raises ValueError on an SSRF refusal, before any other leg.
    sign = await signread_svc.sign_read(client, url)

    # --- book leg (the Score Book for this URL) -------------------
    book = scorebook_svc.score_book(store, index_store, url=url)

    # --- guard leg (the headline verdict) -------------------------
    guard = await guard_svc.guard(client, store, index_store, url)

    # --- recheck leg (records, via the single route's own path) ---
    try:
        recheck = await recheck_svc.recheck(
            client, store, url, watch_id, index_store=index_store
        )
        recheck_section = {**recheck, "recorded": True}
    except UpstreamError as e:
        # The probe failed: the single route's answer would be a
        # bare 502 with nothing recorded. The physical renders the
        # failure as this leg's section — the other legs' reports
        # about the same target are the deliverable — and records
        # nothing, exactly as the single leaves the store untouched.
        recheck_section = {
            "watch_id": (watch_id or "").strip()
            or recheck_svc.default_watch_id(url),
            "url": url,
            "status": "PROBE_FAILED",
            "error": str(e),
            "recorded": False,
            "summary": (
                "The recheck probe failed, so nothing was recorded "
                "and no stored baseline was touched; the next "
                "successful check still diffs against the last "
                "good state."
            ),
        }

    verdict = guard["verdict"]
    recheck_status = recheck_section.get("status")
    headline = (
        f"Guard verdict {verdict} for this endpoint; "
        f"recheck {recheck_status}"
        + (
            " (recorded in the check feed)."
            if recheck_section.get("recorded")
            else " (probe failed — nothing recorded)."
        )
    )
    return {
        "url": url,
        "verdict": verdict,
        "summary": {
            "verdict": verdict,
            "verdict_source": (
                "the guard leg's verdict, verbatim — it already "
                "composes the sign read and the recorded history"
            ),
            "recheck_status": recheck_status,
            "headline": headline,
        },
        "sign": sign,
        "book": book,
        "guard": guard,
        "recheck": recheck_section,
        "limits": LIMITS,
    }
