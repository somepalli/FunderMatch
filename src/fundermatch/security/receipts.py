"""Tamper-evident worker policy receipts."""

from __future__ import annotations

import hmac
import json
from hashlib import sha256


class ReceiptSigner:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("receipt signing secret must contain at least 32 characters")
        self.secret = secret.encode()

    def sign(self, payload: dict[str, object]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hmac.new(self.secret, canonical.encode(), sha256).hexdigest()

    def verify(self, payload: dict[str, object], signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)
