"""
SysEdge licence validation — offline Ed25519 signature check.

Activation flow (one-time, requires internet):
  python3 setup.py --activate <lemon-squeezy-key>
  → calls activation endpoint
  → receives signed licence token
  → writes to .sysedge-licence

All subsequent validation is local using the hardcoded public key.
No internet required after activation.
"""
import json, base64, os, sys
from pathlib import Path
from datetime import datetime, timezone

# Hardcoded public key — embedded in the CLI, cannot be changed without a new release
_PUBLIC_KEY_HEX = "92ec7db55f54dcb6da25e579ab5a1546fe73d9fe5e920251385410b41f933813"

# Licence file location: project root .sysedge-licence
# Also checked: ~/.sysedge-licence (user-level, shared across projects)
_LICENCE_FILENAMES = [".sysedge-licence", Path.home() / ".sysedge-licence"]


def _load_licence_token() -> str | None:
    """Find and return the raw licence token from the project or home directory."""
    # Check environment variable first (CI/CD friendly)
    if token := os.environ.get("SYSEDGE_LICENCE"):
        return token.strip()
    # Check project root (walk up from cwd)
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents)[:3]:
        candidate = parent / ".sysedge-licence"
        if candidate.exists():
            return candidate.read_text().strip()
    # Check home dir
    home_licence = Path.home() / ".sysedge-licence"
    if home_licence.exists():
        return home_licence.read_text().strip()
    return None


def _verify_token(token: str) -> dict | None:
    """Verify the Ed25519 signature and return the payload dict, or None if invalid."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from cryptography.exceptions import InvalidSignature

        pub_bytes = bytes.fromhex(_PUBLIC_KEY_HEX)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

        # Token format: base64(payload_json) . base64(signature)
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig_bytes = base64.urlsafe_b64decode(sig_b64 + "==")

        public_key.verify(sig_bytes, payload_bytes)  # raises InvalidSignature if bad
        return json.loads(payload_bytes)
    except Exception:
        return None


def check_licence(command: str = "") -> bool:
    """
    Check that a valid SysEdge licence is present.

    Returns True if licence is valid and not expired.
    Prints an upgrade message and returns False if not.

    The `command` argument is used in the error message to show what was blocked.
    """
    token = _load_licence_token()
    if not token:
        _print_upgrade(command, "No licence found")
        return False

    payload = _verify_token(token)
    if not payload:
        _print_upgrade(command, "Licence signature invalid")
        return False

    # Check expiry
    expiry_str = payload.get("expiry", "")
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry:
                _print_upgrade(command, f"Licence expired on {expiry_str[:10]}")
                return False
        except ValueError:
            pass

    # Check product
    if payload.get("product") not in ("sysedge-kit", "sysedge-enterprise"):
        _print_upgrade(command, f"Licence is for wrong product: {payload.get('product')}")
        return False

    return True


def licence_info() -> dict | None:
    """Return the licence payload dict if valid, else None. Used by setup/status commands."""
    token = _load_licence_token()
    if not token:
        return None
    return _verify_token(token)


def _print_upgrade(command: str, reason: str):
    print(f"\n  ⚠  This command requires the SysEdge Bootstrap Kit.")
    if reason:
        print(f"     ({reason})")
    print(f"\n     The free CLI includes: briefing, worklog, test-gaps, scan,")
    print(f"     link, create-enhancement, backup, and graph write commands.")
    print(f"\n     Export, import, analyse, and architecture commands require")
    print(f"     the bootstrap kit ($149):")
    print(f"     https://www.org-edge.com/sysedge.html#get-sysedge")
    print()


# ── Activation (called from setup.py --activate <key>) ────────────────────────

def activate(lemon_squeezy_key: str, activation_url: str = "https://sysedge-activation.org-edge.workers.dev") -> bool:
    """
    Exchange a Lemon Squeezy licence key for a signed SysEdge token.

    Calls the activation endpoint once, writes the token to .sysedge-licence.
    After this, all validation is local.
    """
    import urllib.request, urllib.parse

    print(f"  Activating licence key...")
    try:
        data = urllib.parse.urlencode({"key": lemon_squeezy_key}).encode()
        req = urllib.request.Request(activation_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "SysEdge/1.1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        print(f"  ✗  Activation failed: {e}")
        print(f"     Check your licence key or try again later.")
        print(f"     Support: sysedge@org-edge.com")
        return False

    token = result.get("token")
    if not token:
        print(f"  ✗  Activation server returned no token: {result.get('error', 'unknown error')}")
        return False

    # Verify the token before writing it
    payload = _verify_token(token)
    if not payload:
        print(f"  ✗  Token signature invalid — contact sysedge@org-edge.com")
        return False

    # Write to project root
    licence_path = Path.cwd() / ".sysedge-licence"
    licence_path.write_text(token)
    print(f"  ✓  Licence activated: {payload.get('email', 'unknown')}")
    print(f"     Expires: {payload.get('expiry', 'never')[:10]}")
    print(f"     Written to: {licence_path}")
    return True


# ── Development helper: generate a signed token (uses private key) ─────────────

def _sign_token(payload: dict, private_key_hex: str) -> str:
    """Sign a payload dict and return a licence token. Used by the activation server."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    sig_bytes = private_key.sign(payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()
    return f"{payload_b64}.{sig_b64}"
