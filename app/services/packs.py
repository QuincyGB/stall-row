"""Trade packs: POST /v1/pre-trade-pack ($0.10) and
POST /v1/due-diligence-pack ($0.10).

Both compose IN-PROCESS from this service's existing cores — no
fleet-internal HTTP legs, no new data sources:

pre-trade-pack {token, counterparty}:
  token_verdict      — services/verdict.wallet_verdict (the same
                       engine behind POST /v1/wallet-verdict) run
                       on the token contract address.
  counterparty_report — services/counterparty.run_counterparty on
                       the counterparty wallet (token named, so its
                       own legs run as the standard report runs
                       them).
  approval_check     — services/reads.allowance_audit on the
                       counterparty, filtered to approvals whose
                       token is the planned token. Coverage is the
                       audit's own: held tokens x the curated
                       spender list; a token the counterparty does
                       not hold yields no token-specific findings
                       and the section says so.

due-diligence-pack {wallet, token?}:
  counterparty_report, sanctions_check (services/verdict),
  allowance_audit (services/reads), flagged_paytos screen
  (services/registry.build_feed, checked for the wallet), and a
  counterparty-watch-style profile summary
  (services/counterparty_deep.profile_snapshot). The profile leg
  is READ-ONLY: it reads a stored watch snapshot when one exists
  (and says which) and otherwise computes the profile WITHOUT
  storing it — a pack call never overwrites a watch baseline.

Combined lines are computed by stated rules (in each response's
`combined_rule`), never by feel:

pre-trade:  no-go  if the token verdict is AVOID, or any sanctions
            screen in any leg matched;
            caution if the token verdict is CAUTION, or a relevant
            unlimited approval exists, or any leg failed /
            was unavailable;
            go otherwise.
due-diligence: no-go if sanctions matched or the wallet is
            OFAC-flagged in the flagged-paytos feed or profile;
            caution if flagged for any other reason, unlimited
            approvals exist, or any section failed / was
            unavailable; go otherwise.

A leg that raises is captured as {"status": "failed", ...} in its
section — a pack delivers its remaining sections; only a total
failure (every section failed) raises UpstreamError so the
payment does not settle for nothing.
"""

from __future__ import annotations

import time

import httpx

from ..blockscout import Blockscout
from ..rpc import Rpc, UpstreamError
from . import counterparty as counterparty_svc
from . import counterparty_deep as counterparty_deep_svc
from . import reads as reads_svc
from . import registry as registry_svc
from . import verdict as verdict_svc
from .indexfeed import IndexStore

PRE_TRADE_RULE = (
    "no-go if the token verdict is AVOID or any sanctions screen matched; "
    "caution if the token verdict is CAUTION, a relevant unlimited approval "
    "exists, or any leg failed or was unavailable; go otherwise."
)
DUE_DILIGENCE_RULE = (
    "no-go if sanctions matched or the wallet is OFAC-flagged in the "
    "flagged-paytos feed or the profile; caution if flagged for any other "
    "reason, unlimited approvals exist, or any section failed or was "
    "unavailable; go otherwise."
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))


async def _section(coro) -> dict:
    try:
        result = await coro
    except Exception as e:  # noqa: BLE001 — a failed leg is data in a pack
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}
    return {"status": "ok", "result": result}


def _approval_section(audit: dict, token_address: str | None) -> dict:
    """The allowance-audit result, narrowed to one token where the
    caller named one. Coverage honesty: the audit only sees tokens
    the wallet holds (Blockscout) x the curated spender list."""
    if not isinstance(audit, dict):
        return {"status": "failed", "error": "allowance audit returned no data"}
    findings = audit.get("active_approvals")
    if not isinstance(findings, list):
        return {"status": "unavailable", "result": audit}
    relevant = findings
    if token_address:
        relevant = [
            f for f in findings
            if (f.get("token_address") or "").lower() == token_address.lower()
        ]
    return {
        "status": "ok",
        "result": {
            **audit,
            "active_approvals": relevant,
            "relevant_token": token_address,
            "relevant_approvals": relevant,
            "relevant_unlimited_approvals": sum(1 for f in relevant if f.get("unlimited")),
            "all_tokens_active_approvals_count": len(findings),
            "token_filter_note": (
                "Approvals are filtered to the named token. The audit "
                "enumerates tokens the wallet currently holds against "
                "the curated spender list — approvals on a token the "
                "wallet no longer holds, or to spenders outside the "
                "list, are not visible to it."
                if token_address else
                "No token was named, so all active approvals the audit "
                "found are reported."
            ),
        },
    }


# --- pre-trade pack -----------------------------------------------------------

async def run_pre_trade_pack(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    store: IndexStore,
    *,
    token: str,
    counterparty: str,
) -> dict:
    verdict_section = await _section(
        verdict_svc.wallet_verdict(client, rpc, scout, token)
    )
    counterparty_section = await _section(
        counterparty_svc.run_counterparty(
            client, rpc, scout, store,
            pay_to=counterparty, token_address=token,
        )
    )
    audit_raw = await _section(reads_svc.allowance_audit(client, rpc, scout, counterparty))
    if audit_raw["status"] == "ok":
        approval_section = _approval_section(audit_raw["result"], token)
    else:
        approval_section = audit_raw

    sections = {
        "token_verdict": verdict_section,
        "counterparty_report": counterparty_section,
        "approval_check": approval_section,
    }
    if all(s["status"] == "failed" for s in sections.values()):
        raise UpstreamError("pre-trade pack: every leg failed — nothing to deliver")

    reasons: list[str] = []
    verdict_result = verdict_section.get("result") or {}
    token_verdict = verdict_result.get("verdict")
    sanctions_matched = False
    if (verdict_result.get("sanctions") or {}).get("matched"):
        sanctions_matched = True
        reasons.append("token sanctions screen matched (OFAC SDN)")
    cp_legs = (counterparty_section.get("result") or {}).get("legs") or {}
    if ((cp_legs.get("sanctions") or {}).get("result") or {}).get("matched"):
        sanctions_matched = True
        reasons.append("counterparty sanctions screen matched (OFAC SDN)")

    approval_result = approval_section.get("result") or {}
    relevant_unlimited = approval_result.get("relevant_unlimited_approvals") or 0

    degraded = (
        any(s["status"] != "ok" for s in sections.values())
        or bool((counterparty_section.get("result") or {}).get("legs_failed"))
        or bool((counterparty_section.get("result") or {}).get("legs_unavailable"))
    )
    if token_verdict == "AVOID":
        reasons.append("token verdict is AVOID")
    if token_verdict == "CAUTION":
        reasons.append("token verdict is CAUTION")
    if relevant_unlimited:
        reasons.append(f"{relevant_unlimited} relevant unlimited approval(s) on the counterparty for this token")
    if degraded:
        reasons.append("at least one leg failed or was unavailable — the pack is partial")

    if token_verdict == "AVOID" or sanctions_matched:
        combined = "no-go"
    elif token_verdict == "CAUTION" or relevant_unlimited or degraded:
        combined = "caution"
    else:
        combined = "go"
    if not reasons:
        reasons.append("no leg raised a flag under the stated rule")

    return {
        "token": token,
        "counterparty": counterparty,
        "generated_at": _now_iso(),
        "sections": sections,
        "combined_verdict": combined,
        "combined_rule": PRE_TRADE_RULE,
        "reasons": reasons,
        "limits": (
            "The token verdict leg is this service's wallet-verdict engine "
            "(sanctions / age / activity / poisoning) run on the token "
            "contract address — not the separate Token Verdict stall's "
            "contract-risk verdict. The approval leg covers only the "
            "counterparty's currently held tokens against the curated "
            "spender list (see its coverage notes). Legs marked failed "
            "or unavailable delivered nothing."
        ),
    }


# --- due-diligence pack ---------------------------------------------------------

async def run_due_diligence_pack(
    client: httpx.AsyncClient,
    rpc: Rpc,
    scout: Blockscout,
    store: IndexStore,
    *,
    wallet: str,
    token: str | None = None,
    watch_store=None,
) -> dict:
    counterparty_section = await _section(
        counterparty_svc.run_counterparty(
            client, rpc, scout, store, pay_to=wallet, token_address=token,
        )
    )

    sanctions_raw = verdict_svc.sanctions_check(wallet)
    sanctions_section = {
        "status": "unavailable" if sanctions_raw["status"] == "unknown" else "ok",
        "result": sanctions_raw,
    }

    audit_raw = await _section(reads_svc.allowance_audit(client, rpc, scout, wallet))
    allowance_section = (
        _approval_section(audit_raw["result"], token)
        if audit_raw["status"] == "ok" else audit_raw
    )

    # flagged-paytos screen: the registry feed, checked for this wallet
    try:
        feed = registry_svc.build_feed(store)
        entry = next(
            (e for e in feed.get("entries") or [] if e.get("pay_to") == wallet.lower()),
            None,
        )
        flagged_section = {
            "status": "ok",
            "result": {
                "flagged": entry is not None,
                "entry": entry,
                "feed_generated_at": feed.get("generated_at"),
                "feed_counts": feed.get("counts"),
                "feed_limits": feed.get("limits"),
            },
        }
    except Exception as e:  # noqa: BLE001
        flagged_section = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}

    # profile summary — read-only (never overwrites a watch baseline)
    stored = None
    if watch_store is not None:
        try:
            stored = watch_store.get(wallet)
        except Exception:  # noqa: BLE001 — a store read failure is not fatal
            stored = None
    if stored is not None:
        profile_section = {
            "status": "ok",
            "result": {
                "source": "stored_watch_snapshot",
                "stored_snapshot_used": True,
                "profile": stored,
                "note": "The stored counterparty-watch snapshot for this wallet was read; nothing was written.",
            },
        }
    else:
        computed = await _section(
            counterparty_deep_svc.profile_snapshot(client, rpc, scout, store, wallet)
        )
        if computed["status"] == "ok":
            profile_section = {
                "status": "ok",
                "result": {
                    "source": "computed_read_only_not_stored",
                    "stored_snapshot_used": False,
                    "profile": computed["result"],
                    "note": (
                        "No stored watch snapshot exists for this wallet, so "
                        "the profile was computed for this pack only and was "
                        "NOT stored — the counterparty-watch baseline is "
                        "untouched."
                    ),
                },
            }
        else:
            profile_section = computed

    sections = {
        "counterparty_report": counterparty_section,
        "sanctions_check": sanctions_section,
        "allowance_audit": allowance_section,
        "flagged_paytos": flagged_section,
        "profile_summary": profile_section,
    }
    ok_count = sum(1 for s in sections.values() if s["status"] == "ok")
    if ok_count == 0:
        raise UpstreamError("due-diligence pack: every section failed — nothing to deliver")

    reasons: list[str] = []
    sanctions_matched = bool(sanctions_raw.get("matched"))
    if sanctions_matched:
        reasons.append("sanctions screen matched (OFAC SDN)")
    flagged_result = flagged_section.get("result") or {}
    flagged_entry = flagged_result.get("entry") or {}
    flagged_reasons = flagged_entry.get("reasons") or []
    ofac_flagged = "ofac_sdn" in flagged_reasons
    if ofac_flagged:
        reasons.append("wallet is OFAC-flagged in the flagged-paytos feed")
    profile = (profile_section.get("result") or {}).get("profile") or {}
    profile_ofac = any(
        f.get("type") in ("ofac_sdn_match", "ofac_flagged_counterparty")
        for f in (profile.get("flags") or [])
    )
    if profile_ofac:
        reasons.append("profile carries an OFAC flag")
    allowance_result = allowance_section.get("result") or {}
    unlimited = allowance_result.get("unlimited_approvals")
    if not isinstance(unlimited, int):
        unlimited = allowance_result.get("relevant_unlimited_approvals") or 0
    if unlimited:
        reasons.append(f"{unlimited} unlimited approval(s) found")
    if flagged_result.get("flagged") and not ofac_flagged:
        reasons.append(f"wallet is flagged in the flagged-paytos feed ({', '.join(flagged_reasons)})")
    degraded = any(s["status"] != "ok" for s in sections.values())
    if degraded:
        reasons.append("at least one section failed or was unavailable — the pack is partial")

    if sanctions_matched or ofac_flagged or profile_ofac:
        combined, risk = "no-go", "high"
    elif flagged_result.get("flagged") or unlimited or degraded:
        combined, risk = "caution", "medium"
    else:
        combined, risk = "go", "low"
    if not reasons:
        reasons.append("no section raised a flag under the stated rule")

    return {
        "wallet": wallet,
        "token": token,
        "generated_at": _now_iso(),
        "sections": sections,
        "sections_ok": ok_count,
        "combined_verdict": combined,
        "risk_level": risk,
        "combined_rule": DUE_DILIGENCE_RULE,
        "reasons": reasons,
        "limits": (
            "Five sections, each standing on its own sources (see each "
            "section's own limits/coverage). The profile leg is read-only: "
            "it never stores or overwrites a counterparty-watch baseline. "
            "Sections marked failed or unavailable delivered nothing."
        ),
    }
