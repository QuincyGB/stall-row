"""Settings for the stall-row x402 service (one deployment, 12 paid routes)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("stall-row.config")

BASE_MAINNET = "eip155:8453"

# His payout wallet on Base (EVM) — the same payTo Token Verdict,
# RouteVerdict and ListSmith use. Overridable via X402_PAY_TO.
OWNER_WALLET = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
PLACEHOLDER_WALLETS = {"0x0000000000000000000000000000000000000000"}

# ultravioletadao public x402 facilitator (no keys). Verified via /supported:
# serves eip155:8453 (Base) with the exact scheme.
FACILITATOR_URL = "https://facilitator.ultravioletadao.xyz"

# PayAI public x402 facilitator (no keys for the exact scheme). Settling
# through it catalogs these routes in the PayAI index — each facilitator's
# catalog indexes only its own settlements.
PAYAI_FACILITATOR_URL = "https://facilitator.payai.network"

# Public Base JSON-RPC (base.org). Overridable for a keyed/private RPC.
BASE_RPC_URL = "https://mainnet.base.org"

# Blockscout's Base instance. NOTE: from some networks (incl. the build
# sandbox) base.blockscout.com intermittently answers with a Cloudflare
# challenge page instead of JSON. Every Blockscout-backed field degrades
# to "unknown"/omitted when that happens — see app/blockscout.py.
BLOCKSCOUT_BASE_URL = "https://base.blockscout.com"

CDP_DISCOVERY_RESOURCES_URL = (
    "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
)

# Route prices in USDC smallest units (6 decimals) — the single source of
# truth; discovery.py renders the "$x.xx" strings from these.
PRICE_UNITS: dict[str, int] = {
    "/v1/wallet-snapshot": 5_000,      # $0.005
    "/v1/allowance-audit": 5_000,      # $0.005
    "/v1/tx-decoder": 2_000,           # $0.002
    "/v1/gas-oracle": 1_000,           # $0.001
    "/v1/wallet-verdict": 20_000,      # $0.02
    "/v1/tube-search": 10_000,         # $0.01
    "/v1/page-to-markdown": 10_000,    # $0.01
    "/v1/repo-vitals": 5_000,          # $0.005
    "/v1/price-check": 50_000,        # $0.05
    "/v1/card-smith": 100_000,         # $0.10
    "/v1/job-scout": 50_000,           # $0.05
    "/v1/recheck": 5_000,              # $0.005
}


def units_to_price(units: int) -> str:
    """5000 -> '$0.005' (the string format the x402 SDK expects)."""
    dollars = units / 1_000_000
    s = f"{dollars:.6f}".rstrip("0").rstrip(".")
    return f"${s}"


@dataclass(frozen=True)
class Settings:
    pay_to: str
    network: str
    facilitator_url: str
    service_name: str
    port: int
    rpc_url: str
    blockscout_base_url: str
    fetch_timeout_s: float
    github_token: str | None = None
    prices: dict[str, str] = field(default_factory=dict)  # path -> "$x.xx"
    # Coinbase CDP facilitator credentials (optional). Only used when
    # X402_FACILITATOR_URL points at CDP — see use_cdp below.
    cdp_api_key_id: str | None = None
    cdp_api_key_secret: str | None = None
    # Recheck watch state (sqlite). Per-deployment by design: the file
    # lives on the service's disk, so a redeploy re-baselines watches.
    recheck_db_path: str = "recheck.db"

    @property
    def use_cdp(self) -> bool:
        """True when settlements should run through the CDP facilitator
        with JWT auth — the path that feeds the CDP Bazaar catalog."""
        return (
            "api.cdp.coinbase.com" in self.facilitator_url
            and bool(self.cdp_api_key_id)
            and bool(self.cdp_api_key_secret)
        )

    @property
    def use_payai(self) -> bool:
        """True when settlements run through the PayAI facilitator — the
        path that feeds the PayAI index catalog. URL alone selects it."""
        return "facilitator.payai.network" in self.facilitator_url

    def price_for(self, path: str) -> str:
        return self.prices[path]


def load_settings() -> Settings:
    pay_to = os.environ.get("X402_PAY_TO", OWNER_WALLET).strip()
    if pay_to in PLACEHOLDER_WALLETS:
        raise RuntimeError("X402_PAY_TO is a placeholder address — set a real wallet.")
    if not (pay_to.startswith("0x") and len(pay_to) == 42):
        raise RuntimeError(f"X402_PAY_TO does not look like an EVM address: {pay_to!r}")

    prices = {}
    for path, units in PRICE_UNITS.items():
        env_key = "X402_PRICE_" + path.rsplit("/", 1)[-1].replace("-", "_").upper()
        raw = os.environ.get(env_key, "").strip()
        prices[path] = raw if raw else units_to_price(units)

    cdp = {
        k: (os.environ.get(k, "").strip() or None)
        for k in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET")
    }

    return Settings(
        pay_to=pay_to,
        network=os.environ.get("X402_NETWORK", BASE_MAINNET).strip(),
        facilitator_url=os.environ.get("X402_FACILITATOR_URL", FACILITATOR_URL).strip(),
        service_name=os.environ.get("X402_SERVICE_NAME", "stall-row").strip(),
        port=int(os.environ.get("PORT", "3000")),
        rpc_url=os.environ.get("BASE_RPC_URL", BASE_RPC_URL).strip(),
        blockscout_base_url=os.environ.get(
            "BLOCKSCOUT_BASE_URL", BLOCKSCOUT_BASE_URL
        ).strip(),
        fetch_timeout_s=float(os.environ.get("STALLROW_FETCH_TIMEOUT_S", "15")),
        github_token=(os.environ.get("GITHUB_TOKEN", "").strip() or None),
        recheck_db_path=os.environ.get("STALLROW_RECHECK_DB", "recheck.db").strip(),
        prices=prices,
        **{k.lower(): v for k, v in cdp.items() if v is not None},
    )
