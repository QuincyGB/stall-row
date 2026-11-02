"""Route descriptions must fit the CDP facilitator's payload schema cap.

The 402 challenge's `resource.description` is echoed by the buyer into
the x402 v2 payment payload, and the CDP facilitator schema-validates
that payload at /verify. Its ResourceInfo.description cap is 512
characters: on Token Verdict, /v1/verdict (488) settled while
/v1/pre-trade (535) and /v1/batch-verdict (532) were rejected with
"'paymentPayload' is invalid: must match one of
[x402V2PaymentPayload, x402V1PaymentPayload]" — no Base payment could
settle on those two routes from 2026-10-27 (when a runtime-appended
Solana payment clause pushed both descriptions over the cap) until the
descriptions were trimmed on 2026-11-02.

StallRow passes each route's description from the discovery registry
straight into its RouteConfig — nothing is appended at runtime today,
and discovery.py already asserts the raw registry strings stay under
500 chars at import time. This test pins the EMITTED configs (the exact
objects the payment middleware builds challenges from) against the
512-char facilitator cap, so a future runtime transformation of the
description — the exact failure mode that broke Token Verdict — is
caught here instead of at the facilitator. Longest today: 498 chars
(/v1/recheck, /v1/fleet-check/pack, /v1/agent-card-verify).
"""

from __future__ import annotations

import app.main as main
from app.discovery import ROUTES

CDP_DESCRIPTION_CAP = 512


def test_route_configs_cover_every_registry_route():
    # main.routes is built from the discovery registry, so iterating it
    # covers every priced route — including any added later — with no
    # hand-maintained route list here.
    assert set(main.routes) == {f"POST {path}" for path in ROUTES}


def test_route_descriptions_fit_facilitator_cap():
    for route, config in main.routes.items():
        assert config.description is not None, route
        assert len(config.description) <= CDP_DESCRIPTION_CAP, (
            f"{route}: emitted description is {len(config.description)} chars, "
            f"over the CDP facilitator's {CDP_DESCRIPTION_CAP}-char cap — "
            "payments on this route will fail payload validation"
        )
