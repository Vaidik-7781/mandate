"""Key handling for the demo credential provider, user wallet, agent and merchants.

Keys are derived deterministically from `SECRET_SEED` so a restart (or a replay run) reproduces the same
identities. Never use the demo seed outside a sandbox. The agent key is held by `AgentSigner` only and never
enters a model prompt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from verifiable_intent.crypto.signing import private_key_to_jwk, public_key_to_jwk

# Order of the P-256 group
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


@dataclass(frozen=True)
class KeyPair:
    private_key: ec.EllipticCurvePrivateKey
    kid: str

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self.private_key.public_key()

    @property
    def public_jwk(self) -> dict:
        return public_key_to_jwk(self.private_key)

    @property
    def private_jwk(self) -> dict:
        return private_key_to_jwk(self.private_key)


def derive_key(seed: str, label: str, kid: str | None = None) -> KeyPair:
    """Deterministic ES256 key from (seed, label). Demo use only."""
    digest = hashlib.sha256(f"mandate/{label}/{seed}".encode()).digest()
    d = int.from_bytes(digest, "big") % (_N - 1) + 1
    return KeyPair(ec.derive_private_key(d, ec.SECP256R1()), kid or f"{label}-key-1")


def generate_key(kid: str) -> KeyPair:
    return KeyPair(ec.generate_private_key(ec.SECP256R1()), kid)


@dataclass(frozen=True)
class KeyRing:
    issuer: KeyPair  # demo credential provider (signs L1)
    user: KeyPair  # user's device key (signs L2)
    agent: KeyPair  # buyer agent (signs L3a/L3b)

    @classmethod
    def from_seed(cls, seed: str) -> KeyRing:
        return cls(
            issuer=derive_key(seed, "issuer", "mandate-demo-provider-key-1"),
            user=derive_key(seed, "user", "user-device-key-1"),
            agent=derive_key(seed, "agent", "agent-key-1"),
        )


def merchant_key(seed: str, merchant_id: str) -> KeyPair:
    return derive_key(seed, f"merchant/{merchant_id}", f"{merchant_id}-key-1")
