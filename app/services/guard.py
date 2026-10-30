"""Guard: POST /v1/guard ($0.01) — one pay / caution / avoid
verdict for one x402 endpoint, for the BUYER about to pay it.

PulseFeed's /trust proved the shape: a buyer-side agent wants ONE
answer before an irreversible payment, not three reports to
reconcile. This route composes the row's existing inspector tools
into that answer — no new probing machinery, no sibling HTTP calls:

  sign      — services/signread.py on the live endpoint: the 402
              challenge parsed (v1 + v2), normalized terms, and the
              SDK-compatibility verdict. A sign no standard client
              can parse is a hard avoid: the buyer literally cannot
              pay it correctly.
  flags     — the Bad-payTo Registry (services/registry.py): the
              live challenge's payTo screened against the OFAC
              snapshot and the catalogue rotation/vanishing flags,
              with the registry's evidence attached. A flagged
              payTo is a hard avoid.
  history   — services/scorebook.py over this service's own
              stores: the resource's recorded checks, price/payTo
              movement, and catalogue presence. A resource with no
              record is NOT called safe — unknown history caps the
              verdict at CAUTION, the pre-trade guard's rule.

Verdict bands mirror Pre-Trade Guard: PROCEED / CAUTION / ABORT,
hard aborts override everything, and every verdict carries its
reasons — a verdict without reasons is not delivered. The one
probe inside is the sign reader's unpaid probe: this route never
pays, signs, or settles the target it judges.
"""

from __future__ import annotations

import logging

import httpx

from . import registry as registry_svc
from . import scorebook as scorebook_svc
from . import signread as signread_svc

log = logging.getLogger("stall-row.guard")

PRICE_UNITS = 10_000  # $0.01 — mirrors config.PRICE_UNITS

LIMITS = (
    "One unpaid probe plus this service's own records, at call "
    "time. The flags leg covers only the registry's two sources "
    "(the bundled OFAC snapshot and the newest catalogue snapshot "
    "pair) — a payTo absent from them is 'not flagged here', never "
    "'clean everywhere'. History covers the score book's windows "
    "(see its limits). Delivery quality after payment is a "
    "different question — see the recheck stall and Settled "
    "delivery stamps."
)


async def guard(client: httpx.AsyncClient, store, index_store, url: str) -> dict:
    """Compose the verdict. Raises ValueError only when the sign
    reader's SSRF guard refuses the target (the handler's 422)."""
    sign = await signread_svc.sign_read(client, url)
    reasons: list[dict] = []
    hard_abort = False
    caution = False

    # --- sign leg -------------------------------------------------
    first = (sign.get("accepts") or [None])[0]
    if not sign.get("challenge_found"):
        hard_abort = True
        detail = (
            sign["defects"][0]["detail"]
            if sign.get("defects")
            else "no readable 402 challenge"
        )
        reasons.append({"code": "no_readable_sign", "detail": detail})
    else:
        compat = sign.get("sdk_compat") or {}
        python_ok = (compat.get("python") or {}).get("parses")
        if python_ok is False:
            hard_abort = True
            failures = "; ".join(
                (compat.get("python") or {}).get("failures") or []
            )
            reasons.append(
                {
                    "code": "sdk_unparseable",
                    "detail": f"standard clients cannot resolve this sign: {failures}",
                }
            )
        for mismatch in sign.get("mismatches") or []:
            caution = True
            reasons.append(
                {
                    "code": "declared_mismatch",
                    "detail": (
                        f"declared {mismatch.get('field')} "
                        f"{mismatch.get('declared')!r} != live "
                        f"{mismatch.get('live')!r}"
                    ),
                }
            )

    # --- flags leg --------------------------------------------------
    flagged_entry = None
    pay_to = first.get("payTo") if first else None
    if pay_to:
        feed = registry_svc.build_feed(index_store)
        for entry in feed.get("entries") or []:
            if entry.get("pay_to") == pay_to.lower():
                flagged_entry = entry
                break
        if flagged_entry is not None:
            hard_abort = True
            reasons.append(
                {
                    "code": "payto_flagged",
                    "detail": (
                        "payTo is in the bad-payTo registry: "
                        + ", ".join(flagged_entry.get("reasons") or [])
                    ),
                }
            )
        else:
            reasons.append(
                {
                    "code": "payto_not_flagged",
                    "detail": "payTo absent from the bad-payTo registry's sources",
                }
            )

    # --- history leg --------------------------------------------------
    book = scorebook_svc.score_book(store, index_store, url=url)
    live = book.get("live_checks") or {}
    if not book.get("found"):
        caution = True
        reasons.append(
            {
                "code": "no_history",
                "detail": "no recorded checks or catalogue presence in our stores — unexamined is never called safe",
            }
        )
    else:
        if (live.get("payto_changes") or 0) > 0:
            caution = True
            reasons.append(
                {
                    "code": "payto_moved_before",
                    "detail": (
                        f"this resource has carried {live['payto_changes'] + 1} "
                        "distinct payTos across recorded checks"
                    ),
                }
            )
        if (live.get("price_changes") or 0) > 0:
            reasons.append(
                {
                    "code": "price_moved_before",
                    "detail": (
                        f"price changed {live['price_changes']} time(s) across "
                        f"{live.get('checks', 0)} recorded checks"
                    ),
                }
            )

    verdict = "ABORT" if hard_abort else ("CAUTION" if caution else "PROCEED")
    if verdict == "PROCEED":
        reasons.append(
            {
                "code": "all_legs_clear",
                "detail": "sign parses for standard clients, payTo unflagged, history on record with no payTo movement",
            }
        )
    return {
        "url": url,
        "verdict": verdict,
        "reasons": reasons,
        "terms": first,
        "sign": {
            "challenge_found": sign.get("challenge_found"),
            "x402_version": sign.get("x402_version"),
            "http_status": sign.get("http_status"),
            "sdk_compat": sign.get("sdk_compat"),
            "defects": sign.get("defects"),
        },
        "payto_flag": flagged_entry,
        "history": {
            "found": book.get("found"),
            "live_checks": live,
            "catalogue": book.get("catalogue"),
        },
        "limits": LIMITS,
    }
