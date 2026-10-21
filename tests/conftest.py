import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Sandbox/CI shells may export a no_proxy list containing bracketed IPv6
# entries ("[::1]"). The x402 SDK's httpx-based facilitator client parses
# that list as URLs and dies with "Invalid port: ':1]'", which makes the
# paywall middleware return 500 instead of 402 in tests. Production
# (Railway) exports no such variable. Drop just the bracketed entries so
# the suite tests the code, not the shell's proxy config.
for _var in ("no_proxy", "NO_PROXY"):
    _val = os.environ.get(_var)
    if _val:
        _kept = [e for e in _val.split(",") if "[" not in e and "]" not in e]
        os.environ[_var] = ",".join(_kept)
