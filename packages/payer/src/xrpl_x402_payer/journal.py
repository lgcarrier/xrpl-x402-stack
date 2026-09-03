from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from x402.extensions.payment_identifier import extract_payment_identifier
from x402.schemas import PaymentPayload

ATTEMPT_LEASE_SECONDS = 30.0
# A completed attempt is retained briefly so callers that were concurrently
# waiting on the same fingerprint can observe and reuse the exact payload.
# Pending/ready attempts are retained until a terminal result is recorded.
TERMINAL_REUSE_SECONDS = 300.0
JOURNAL_VERSION = 1


class PaymentAttempt(BaseModel):
    """Transient exact signed payload state stored outside receipt history."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    payer: str
    recovery_scope: str
    owner_token: str
    state: Literal["claimed", "ready", "pending", "paid"]
    lease_expires_at: float
    payment_payload: PaymentPayload | None = None
    payment_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    attempt: PaymentAttempt
    owner_token: str | None

    @property
    def is_owner(self) -> bool:
        return self.owner_token is not None


class ReceiptJournal:
    """Locked JSONL receipts plus an atomically replaced attempt sidecar."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.index_path = Path(f"{path}.attempts.json")
        self.lock_path = Path(f"{path}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        with os.fdopen(descriptor, "r+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _load_index_unlocked(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": JOURNAL_VERSION, "attempts": {}}
        with self.index_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != JOURNAL_VERSION
            or not isinstance(loaded.get("attempts"), dict)
        ):
            raise ValueError("Invalid x402 payer attempt index")
        return loaded

    def _write_index_unlocked(self, index: dict[str, Any]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.index_path.parent,
            prefix=f".{self.index_path.name}.",
            suffix=".tmp",
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(index, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index_path)
            directory = os.open(self.index_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _attempt_from_index(
        index: dict[str, Any], fingerprint: str
    ) -> PaymentAttempt | None:
        raw = index["attempts"].get(fingerprint)
        if raw is None:
            return None
        attempt = PaymentAttempt.model_validate(raw)
        if (
            attempt.payment_payload is not None
            and extract_payment_identifier(attempt.payment_payload)
            != attempt.payment_identifier
        ):
            raise ValueError(
                "Stored payment identifier does not match its exact payload"
            )
        return attempt

    @staticmethod
    def _put_attempt(
        index: dict[str, Any], attempt: PaymentAttempt
    ) -> None:
        index["attempts"][attempt.fingerprint] = attempt.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    @staticmethod
    def _prune_expired_paid_unlocked(
        index: dict[str, Any], now: float
    ) -> int:
        expired: list[str] = []
        for fingerprint, raw in index["attempts"].items():
            if not isinstance(raw, dict) or raw.get("state") != "paid":
                continue
            try:
                expiry = float(raw.get("lease_expires_at", 0.0))
            except (TypeError, ValueError):
                continue
            if expiry <= now:
                expired.append(fingerprint)
        for fingerprint in expired:
            index["attempts"].pop(fingerprint, None)
        return len(expired)

    def prune_expired_attempts(self) -> int:
        """Remove transient terminal payloads after their reuse window."""

        with self._locked():
            index = self._load_index_unlocked()
            count = self._prune_expired_paid_unlocked(index, time.time())
            if count:
                self._write_index_unlocked(index)
            return count

    def claim_attempt(
        self,
        *,
        fingerprint: str,
        payer: str,
        recovery_scope: str,
        lease_seconds: float = ATTEMPT_LEASE_SECONDS,
    ) -> AttemptClaim:
        if not recovery_scope.strip():
            raise ValueError("recovery_scope must be non-empty")
        now = time.time()
        with self._locked():
            index = self._load_index_unlocked()
            pruned = self._prune_expired_paid_unlocked(index, now)
            existing = self._attempt_from_index(index, fingerprint)
            if existing is not None:
                if (
                    existing.payer != payer
                    or existing.recovery_scope != recovery_scope
                ):
                    raise ValueError("Payment attempt recovery scope mismatch")
                if existing.payment_payload is not None:
                    if pruned:
                        self._write_index_unlocked(index)
                    return AttemptClaim(existing, None)
                if existing.lease_expires_at > now:
                    if pruned:
                        self._write_index_unlocked(index)
                    return AttemptClaim(existing, None)

            token = uuid4().hex
            claimed = PaymentAttempt(
                fingerprint=fingerprint,
                payer=payer,
                recovery_scope=recovery_scope,
                owner_token=token,
                state="claimed",
                lease_expires_at=now + lease_seconds,
            )
            self._put_attempt(index, claimed)
            self._write_index_unlocked(index)
            return AttemptClaim(claimed, token)

    def get_attempt(self, fingerprint: str) -> PaymentAttempt | None:
        with self._locked():
            return self._attempt_from_index(
                self._load_index_unlocked(), fingerprint
            )

    def persist_attempt_payload(
        self,
        *,
        fingerprint: str,
        owner_token: str | None,
        payment_payload: PaymentPayload,
        payment_identifier: str | None,
    ) -> PaymentAttempt:
        with self._locked():
            index = self._load_index_unlocked()
            current = self._attempt_from_index(index, fingerprint)
            if current is None:
                raise RuntimeError("Payment attempt claim disappeared")
            if current.payment_payload is not None:
                return current
            if owner_token is None or current.owner_token != owner_token:
                raise RuntimeError("Payment attempt lease is owned by another payer")
            ready = current.model_copy(
                update={
                    "state": "ready",
                    "lease_expires_at": 0.0,
                    "payment_payload": payment_payload,
                    "payment_identifier": payment_identifier,
                }
            )
            self._put_attempt(index, ready)
            self._write_index_unlocked(index)
            return ready

    def release_claim(self, fingerprint: str, owner_token: str | None) -> None:
        if owner_token is None:
            return
        with self._locked():
            index = self._load_index_unlocked()
            current = self._attempt_from_index(index, fingerprint)
            if (
                current is not None
                and current.owner_token == owner_token
                and current.payment_payload is None
            ):
                index["attempts"].pop(fingerprint, None)
                self._write_index_unlocked(index)

    @staticmethod
    def _receipt_key(receipt: Any) -> tuple[str, str, str] | None:
        fingerprint = receipt.request_fingerprint
        if not fingerprint:
            return None
        return (
            fingerprint,
            receipt.state,
            str(receipt.settlement.transaction or ""),
        )

    def _records_unlocked(self) -> Iterator[Any]:
        from xrpl_x402_payer.receipts import ReceiptRecord

        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                if raw_line.strip():
                    yield ReceiptRecord.model_validate_json(raw_line)

    def _append_unlocked(self, receipt: Any) -> bool:
        key = self._receipt_key(receipt)
        if key is not None and any(
            self._receipt_key(existing) == key
            for existing in self._records_unlocked()
        ):
            return False
        encoded = (receipt.model_dump_json(by_alias=True) + "\n").encode("utf-8")
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("Unable to append x402 receipt")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True

    def append(self, receipt: Any) -> bool:
        with self._locked():
            return self._append_unlocked(receipt)

    def record_outcome(
        self,
        receipt: Any,
        *,
        terminal_reuse_seconds: float = TERMINAL_REUSE_SECONDS,
    ) -> bool:
        fingerprint = receipt.request_fingerprint
        with self._locked():
            if fingerprint and receipt.state != "paid":
                transaction = str(receipt.settlement.transaction or "")
                for existing in self._records_unlocked():
                    if (
                        existing.request_fingerprint == fingerprint
                        and str(existing.settlement.transaction or "")
                        == transaction
                        and existing.state in {"paid", "failed"}
                    ):
                        # A late pending/failure response from a concurrent
                        # caller cannot regress an already terminal attempt.
                        return False
            appended = self._append_unlocked(receipt)
            if not fingerprint:
                return appended
            index = self._load_index_unlocked()
            attempt = self._attempt_from_index(index, fingerprint)
            if attempt is None:
                return appended
            if receipt.state == "pending":
                updated = attempt.model_copy(
                    update={"state": "pending", "lease_expires_at": 0.0}
                )
                self._put_attempt(index, updated)
            elif receipt.state == "paid":
                updated = attempt.model_copy(
                    update={
                        "state": "paid",
                        "lease_expires_at": time.time()
                        + terminal_reuse_seconds,
                    }
                )
                self._put_attempt(index, updated)
            else:
                index["attempts"].pop(fingerprint, None)
            self._write_index_unlocked(index)
            return appended

    def list(self, limit: int = 10) -> list[Any]:
        if limit <= 0:
            return []
        with self._locked():
            records: deque[Any] = deque(maxlen=limit)
            records.extend(self._records_unlocked())
            return list(reversed(records))

    def all(self) -> list[Any]:
        with self._locked():
            return list(self._records_unlocked())

    def latest_for_fingerprint(self, request_fingerprint: str) -> Any | None:
        with self._locked():
            latest = None
            for record in self._records_unlocked():
                if record.request_fingerprint == request_fingerprint:
                    latest = record
            return latest
