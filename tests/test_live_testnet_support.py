from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import devtools.live_testnet_support as support
from xrpl.wallet import Wallet


def build_demo_wallet_set() -> support.DemoWalletSet:
    return support.DemoWalletSet(
        merchant_wallet=Wallet.create(),
        buyers={
            "xrp": Wallet.create(),
            "rlusd": Wallet.create(),
            "usdc": Wallet.create(),
        },
    )


def test_load_rlusd_claim_state_migrates_v1_payload(tmp_path) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    claim_state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "canonical_wallet_address": "rCanonical",
                "issuer": "rIssuer",
                "last_successful_claim_at": "2026-03-12T00:00:00+00:00",
                "last_successful_claim_tx_hash": "CLAIMHASH",
            }
        )
    )

    state = support.load_rlusd_claim_state(
        claim_state_path,
        "rCanonical",
        "rIssuer",
    )

    assert state.canonical_wallet_address == "rCanonical"
    assert state.issuer == "rIssuer"
    assert state.last_successful_session_claim_at == datetime(
        2026,
        3,
        12,
        tzinfo=timezone.utc,
    )
    assert state.last_successful_session_claim_tx_hash == "CLAIMHASH"
    assert state.claim_wallets == []


def test_get_demo_wallet_set_upgrades_v1_cache_and_preserves_xrp_pair(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "xrpl-testnet-wallets.json"
    merchant_wallet = Wallet.create()
    xrp_buyer_wallet = Wallet.create()
    rlusd_buyer_wallet = Wallet.create()
    usdc_buyer_wallet = Wallet.create()
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet_a": {
                    "classic_address": merchant_wallet.classic_address,
                    "seed": merchant_wallet.seed,
                },
                "wallet_b": {
                    "classic_address": xrp_buyer_wallet.classic_address,
                    "seed": xrp_buyer_wallet.seed,
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(support, "wallet_cache_path", lambda: cache_path)
    monkeypatch.setattr(support, "_account_exists", lambda _client, _address: True)
    generated_wallets = {
        f"{support.DEMO_WALLET_USAGE_CONTEXT_PREFIX}-buyer-rlusd": rlusd_buyer_wallet,
        f"{support.DEMO_WALLET_USAGE_CONTEXT_PREFIX}-buyer-usdc": usdc_buyer_wallet,
    }
    monkeypatch.setattr(
        support,
        "generate_faucet_wallet",
        lambda _client, usage_context: generated_wallets[usage_context],
    )

    demo_wallets = support.get_demo_wallet_set(client="client")
    live_pair = support.get_live_wallet_pair(client="client")
    cached_demo_wallets = support.load_cached_demo_wallet_set(cache_path)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))

    assert demo_wallets.merchant_wallet.classic_address == merchant_wallet.classic_address
    assert demo_wallets.buyer_wallet("xrp").classic_address == xrp_buyer_wallet.classic_address
    assert demo_wallets.buyer_wallet("rlusd").classic_address == rlusd_buyer_wallet.classic_address
    assert demo_wallets.buyer_wallet("usdc").classic_address == usdc_buyer_wallet.classic_address
    assert cached_demo_wallets is not None
    assert live_pair.wallet_a.classic_address == merchant_wallet.classic_address
    assert live_pair.wallet_b.classic_address == xrp_buyer_wallet.classic_address
    assert payload["version"] == 2
    assert payload["merchant"]["classic_address"] == merchant_wallet.classic_address
    assert payload["buyers"]["xrp"]["classic_address"] == xrp_buyer_wallet.classic_address
    assert payload["buyers"]["rlusd"]["classic_address"] == rlusd_buyer_wallet.classic_address
    assert payload["buyers"]["usdc"]["classic_address"] == usdc_buyer_wallet.classic_address


def test_recover_tracked_claim_wallets_sweeps_and_deletes_wallet(tmp_path, monkeypatch) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    accumulator_wallet = Wallet.create()
    disposable_wallet = Wallet.create()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    state = support.RLUSDClaimState(
        canonical_wallet_address=accumulator_wallet.classic_address,
        issuer="rIssuer",
        claim_wallets=[
            support.ClaimWalletState(
                classic_address=disposable_wallet.classic_address,
                seed=disposable_wallet.seed,
                created_at=now - timedelta(hours=1),
                claim_tx_hash="CLAIMTX",
                status=support.CLAIM_WALLET_STATUS_CLAIMED,
            )
        ],
    )
    support.write_rlusd_claim_state(claim_state_path, state)

    trustlines: dict[str, dict[str, str] | None] = {
        accumulator_wallet.classic_address: {"balance": "0", "limit": "100000", "account": "rIssuer", "currency": "RLUSD"},
        disposable_wallet.classic_address: {"balance": "6.25", "limit": "100000", "account": "rIssuer", "currency": "RLUSD"},
    }
    xrp_balances = {
        accumulator_wallet.classic_address: 0,
        disposable_wallet.classic_address: 99_999_970,
    }

    monkeypatch.setattr(support, "ensure_rlusd_trustline", lambda _client, _wallet, _issuer: None)
    monkeypatch.setattr(
        support,
        "get_validated_account_root",
        lambda _client, address: {
            "account_data": {"Balance": str(xrp_balances[address]), "Sequence": "100"},
            "ledger_index": 400,
        },
    )
    monkeypatch.setattr(
        support,
        "get_validated_trustline",
        lambda _client, address, _issuer, currency_code="RLUSD": trustlines[address],
    )

    def fake_submit_payment(_client, wallet, destination_address, _issuer, amount):
        trustlines[wallet.classic_address] = {"balance": "0", "limit": "100000", "account": "rIssuer", "currency": "RLUSD"}
        trustlines[destination_address] = {
            "balance": str(Decimal(trustlines[destination_address]["balance"]) + amount),
            "limit": "100000",
            "account": "rIssuer",
            "currency": "RLUSD",
        }
        return "SWEEPTX"

    monkeypatch.setattr(support, "submit_validated_rlusd_payment", fake_submit_payment)
    monkeypatch.setattr(
        support,
        "wait_for_trustline_balance_increase",
        lambda _client, address, _issuer, *, starting_balance, increase, timeout_seconds=30: (
            Decimal(trustlines[address]["balance"])
        ),
    )
    monkeypatch.setattr(
        support,
        "reset_rlusd_trustline",
        lambda _client, wallet, _issuer: (
            trustlines.__setitem__(
                wallet.classic_address,
                {"balance": "0", "limit": "0", "account": "rIssuer", "currency": "RLUSD"},
            )
            or "RESETTX"
        ),
    )
    monkeypatch.setattr(
        support,
        "wait_for_trustline_removal",
        lambda _client, address, _issuer, timeout_seconds=30: trustlines.__setitem__(address, None) is None,
    )
    monkeypatch.setattr(support, "submit_validated_account_delete", lambda _client, _wallet, _destination: "DELETETX")

    recovered_state, summary = support.recover_tracked_claim_wallets(
        client=None,
        accumulator_wallet=accumulator_wallet,
        issuer="rIssuer",
        claim_state_file=claim_state_path,
        now=now,
    )

    record = recovered_state.claim_wallets[0]
    assert summary.recovered_rlusd_amount == Decimal("6.25")
    assert summary.deleted_wallets == 1
    assert record.status == support.CLAIM_WALLET_STATUS_DELETED
    assert record.rlusd_sweep_tx_hash == "SWEEPTX"
    assert record.trustline_reset_tx_hash == "RESETTX"
    assert record.account_delete_tx_hash == "DELETETX"
    assert record.last_known_rlusd_balance == Decimal("0")
    assert record.last_known_xrp_balance_drops == 0
    assert record.deleted_at == now


def test_recover_tracked_claim_wallets_leaves_wallet_pending_delete_until_eligible(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    accumulator_wallet = Wallet.create()
    disposable_wallet = Wallet.create()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    state = support.RLUSDClaimState(
        canonical_wallet_address=accumulator_wallet.classic_address,
        issuer="rIssuer",
        claim_wallets=[
            support.ClaimWalletState(
                classic_address=disposable_wallet.classic_address,
                seed=disposable_wallet.seed,
                created_at=now - timedelta(minutes=5),
                claim_tx_hash="CLAIMTX",
                status=support.CLAIM_WALLET_STATUS_CLAIMED,
            )
        ],
    )
    support.write_rlusd_claim_state(claim_state_path, state)

    monkeypatch.setattr(
        support,
        "get_validated_account_root",
        lambda _client, _address: {
            "account_data": {"Balance": "99999970", "Sequence": "300"},
            "ledger_index": 500,
        },
    )
    monkeypatch.setattr(
        support,
        "get_validated_trustline",
        lambda _client, _address, _issuer, currency_code="RLUSD": None,
    )

    recovered_state, summary = support.recover_tracked_claim_wallets(
        client=None,
        accumulator_wallet=accumulator_wallet,
        issuer="rIssuer",
        claim_state_file=claim_state_path,
        now=now,
    )

    record = recovered_state.claim_wallets[0]
    assert summary.deleted_wallets == 0
    assert record.status == support.CLAIM_WALLET_STATUS_RLUSD_SWEPT
    assert record.account_delete_tx_hash is None
    assert "validated ledger 557" in str(record.last_error)


def test_claim_rlusd_topup_returns_missing_token_after_recovery(tmp_path, monkeypatch) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    wallets = support.LiveWalletPair(wallet_a=Wallet.create(), wallet_b=Wallet.create())
    recovered_state = support.RLUSDClaimState(
        canonical_wallet_address=wallets.wallet_a.classic_address,
        issuer="rIssuer",
    )
    recovery_summary = support.ClaimWalletRecoverySummary(
        recovered_rlusd_amount=Decimal("3.25"),
        deleted_wallets=2,
        processed_wallets=2,
    )
    faucet_called = {"value": False}

    monkeypatch.setattr(
        support,
        "recover_tracked_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            recovered_state,
            recovery_summary,
        ),
    )
    monkeypatch.setattr(support, "ensure_rlusd_trustline", lambda _client, _wallet, _issuer: None)
    monkeypatch.setattr(
        support,
        "generate_faucet_wallet",
        lambda *_args, **_kwargs: faucet_called.__setitem__("value", True),
    )

    result = support.claim_rlusd_topup(
        client=None,
        wallets=wallets,
        issuer="rIssuer",
        session_token=None,
        now=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
        claim_state_file=claim_state_path,
    )

    assert result.status == "missing_token"
    assert result.swept_amount == Decimal("3.25")
    assert result.deleted_wallets == 2
    assert faucet_called["value"] is False


def test_claim_rlusd_topup_bridges_accumulator_balance_to_rlusd_buyer(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    wallet_set = build_demo_wallet_set()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)
    balances = {
        wallet_set.merchant_wallet.classic_address: Decimal("3.25"),
        wallet_set.buyer_wallet("rlusd").classic_address: Decimal("0"),
    }
    ensure_calls: list[str] = []

    monkeypatch.setattr(
        support,
        "recover_tracked_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.RLUSDClaimState(
                canonical_wallet_address=wallet_set.merchant_wallet.classic_address,
                issuer="rIssuer",
            ),
            support.ClaimWalletRecoverySummary(
                recovered_rlusd_amount=Decimal("3.25"),
                deleted_wallets=1,
                processed_wallets=1,
            ),
        ),
    )
    monkeypatch.setattr(
        support,
        "ensure_rlusd_trustline",
        lambda _client, wallet, _issuer, limit_value="100000": ensure_calls.append(
            wallet.classic_address
        )
        or None,
    )
    monkeypatch.setattr(
        support,
        "get_validated_trustline_balance",
        lambda _client, address, _issuer, currency_code=support.RLUSD_CODE: balances.get(
            address, Decimal("0")
        ),
    )

    def fake_submit_payment(_client, wallet, destination_address, _issuer, amount):
        balances[wallet.classic_address] -= amount
        balances[destination_address] = balances.get(destination_address, Decimal("0")) + amount
        return "BRIDGETX"

    monkeypatch.setattr(support, "submit_validated_rlusd_payment", fake_submit_payment)
    monkeypatch.setattr(
        support,
        "wait_for_trustline_balance_increase",
        lambda _client, _address, _issuer, *, starting_balance, increase, timeout_seconds=30, currency_code=support.RLUSD_CODE: starting_balance
        + increase,
    )

    result = support.claim_rlusd_topup(
        client=None,
        wallets=wallet_set,
        issuer="rIssuer",
        session_token=None,
        now=now,
        claim_state_file=claim_state_path,
    )

    assert result.status == "missing_token"
    assert result.buyer_wallet_address == wallet_set.buyer_wallet("rlusd").classic_address
    assert result.bridged_amount == Decimal("3.25")
    assert balances[wallet_set.merchant_wallet.classic_address] == Decimal("0")
    assert balances[wallet_set.buyer_wallet("rlusd").classic_address] == Decimal("3.25")
    assert ensure_calls == [
        wallet_set.merchant_wallet.classic_address,
        wallet_set.buyer_wallet("rlusd").classic_address,
    ]


def test_claim_rlusd_topup_returns_hard_failure_when_buyer_bridge_fails(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    wallet_set = build_demo_wallet_set()
    balances = {
        wallet_set.merchant_wallet.classic_address: Decimal("4.50"),
        wallet_set.buyer_wallet("rlusd").classic_address: Decimal("0"),
    }

    monkeypatch.setattr(
        support,
        "recover_tracked_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.RLUSDClaimState(
                canonical_wallet_address=wallet_set.merchant_wallet.classic_address,
                issuer="rIssuer",
            ),
            support.ClaimWalletRecoverySummary(recovered_rlusd_amount=Decimal("4.50")),
        ),
    )
    monkeypatch.setattr(support, "ensure_rlusd_trustline", lambda _client, _wallet, _issuer, limit_value="100000": None)
    monkeypatch.setattr(
        support,
        "get_validated_trustline_balance",
        lambda _client, address, _issuer, currency_code=support.RLUSD_CODE: balances.get(
            address, Decimal("0")
        ),
    )
    monkeypatch.setattr(
        support,
        "submit_validated_rlusd_payment",
        lambda _client, _wallet, _destination_address, _issuer, _amount: (_ for _ in ()).throw(
            RuntimeError("bridge tx failed")
        ),
    )

    result = support.claim_rlusd_topup(
        client=None,
        wallets=wallet_set,
        issuer="rIssuer",
        session_token=None,
        claim_state_file=claim_state_path,
    )

    assert result.status == "bridge_failed"
    assert result.buyer_wallet_address == wallet_set.buyer_wallet("rlusd").classic_address
    assert "Failed to fund buyer wallet" in result.message
    assert "4.50 RLUSD remain on accumulator wallet" in result.message


def test_claim_rlusd_topup_creates_disposable_wallet_and_persists_success(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    wallets = support.LiveWalletPair(wallet_a=Wallet.create(), wallet_b=Wallet.create())
    disposable_wallet = Wallet.create()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        support,
        "recover_tracked_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.RLUSDClaimState(
                canonical_wallet_address=wallets.wallet_a.classic_address,
                issuer="rIssuer",
            ),
            support.ClaimWalletRecoverySummary(),
        ),
    )

    def fake_ensure(_client, wallet, _issuer, limit_value="100000"):
        if wallet.classic_address == wallets.wallet_a.classic_address:
            return None
        return "TRUSTTX"

    monkeypatch.setattr(support, "ensure_rlusd_trustline", fake_ensure)
    monkeypatch.setattr(support, "generate_faucet_wallet", lambda *_args, **_kwargs: disposable_wallet)
    monkeypatch.setattr(support, "mint_rlusd_to_address", lambda _address, _token: "CLAIMHASH")
    monkeypatch.setattr(
        support,
        "wait_for_trustline_balance_increase",
        lambda _client, _address, _issuer, *, starting_balance, increase, timeout_seconds=30: starting_balance
        + increase,
    )

    def fake_recover(_client, claim_wallet, _accumulator_wallet, _issuer, now):
        claim_wallet.rlusd_sweep_tx_hash = "SWEEPTX"
        claim_wallet.trustline_reset_tx_hash = "RESETTX"
        claim_wallet.account_delete_tx_hash = "DELETETX"
        claim_wallet.status = support.CLAIM_WALLET_STATUS_DELETED
        claim_wallet.deleted_at = now
        claim_wallet.last_known_rlusd_balance = Decimal("0")
        claim_wallet.last_known_xrp_balance_drops = 0
        return True, support.ClaimWalletRecoverySummary(
            recovered_rlusd_amount=Decimal("10"),
            deleted_wallets=1,
            processed_wallets=1,
        )

    monkeypatch.setattr(support, "_recover_claim_wallet", fake_recover)

    result = support.claim_rlusd_topup(
        client=None,
        wallets=wallets,
        issuer="rIssuer",
        session_token="session-token",
        now=now,
        claim_state_file=claim_state_path,
    )

    persisted_state = support.load_rlusd_claim_state(
        claim_state_path,
        wallets.wallet_a.classic_address,
        "rIssuer",
    )
    record = persisted_state.claim_wallets[0]

    assert result.status == "claimed"
    assert result.claim_tx_hash == "CLAIMHASH"
    assert result.created_claim_wallet_address == disposable_wallet.classic_address
    assert persisted_state.last_claim_attempt_at == now
    assert persisted_state.last_successful_session_claim_at == now
    assert persisted_state.last_successful_session_claim_tx_hash == "CLAIMHASH"
    assert record.classic_address == disposable_wallet.classic_address
    assert record.trustline_create_tx_hash == "TRUSTTX"
    assert record.claim_tx_hash == "CLAIMHASH"
    assert record.status == support.CLAIM_WALLET_STATUS_DELETED
    assert record.account_delete_tx_hash == "DELETETX"


def test_claim_rlusd_topup_records_failed_wallet_on_rate_limit(tmp_path, monkeypatch) -> None:
    claim_state_path = tmp_path / "rlusd-claim-state.json"
    wallets = support.LiveWalletPair(wallet_a=Wallet.create(), wallet_b=Wallet.create())
    disposable_wallet = Wallet.create()
    previous_claim_at = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
    now = previous_claim_at + timedelta(hours=25)
    existing_state = support.RLUSDClaimState(
        canonical_wallet_address=wallets.wallet_a.classic_address,
        issuer="rIssuer",
        last_successful_session_claim_at=previous_claim_at,
        last_successful_session_claim_tx_hash="OLDHASH",
    )

    monkeypatch.setattr(
        support,
        "recover_tracked_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            existing_state,
            support.ClaimWalletRecoverySummary(),
        ),
    )
    monkeypatch.setattr(
        support,
        "ensure_rlusd_trustline",
        lambda _client, _wallet, _issuer, limit_value="100000": "TRUSTTX",
    )
    monkeypatch.setattr(support, "generate_faucet_wallet", lambda *_args, **_kwargs: disposable_wallet)
    monkeypatch.setattr(
        support,
        "mint_rlusd_to_address",
        lambda _address, _token: (_ for _ in ()).throw(
            support.RLUSDMintRateLimitedError("RLUSD faucet rate limited: too soon")
        ),
    )
    monkeypatch.setattr(
        support,
        "_recover_claim_wallet",
        lambda _client, claim_wallet, _accumulator_wallet, _issuer, now: (
            claim_wallet.__setattr__("status", support.CLAIM_WALLET_STATUS_CLAIM_FAILED)
            or False,
            support.ClaimWalletRecoverySummary(processed_wallets=1),
        ),
    )

    result = support.claim_rlusd_topup(
        client=None,
        wallets=wallets,
        issuer="rIssuer",
        session_token="session-token",
        now=now,
        claim_state_file=claim_state_path,
    )

    persisted_state = support.load_rlusd_claim_state(
        claim_state_path,
        wallets.wallet_a.classic_address,
        "rIssuer",
    )
    record = persisted_state.claim_wallets[0]

    assert result.status == "rate_limited"
    assert persisted_state.last_successful_session_claim_at == previous_claim_at
    assert persisted_state.last_successful_session_claim_tx_hash == "OLDHASH"
    assert record.classic_address == disposable_wallet.classic_address
    assert record.claim_attempted_at == now
    assert record.status == support.CLAIM_WALLET_STATUS_CLAIM_FAILED
    assert record.last_error == "RLUSD faucet rate limited: too soon"


def test_write_and_load_usdc_claim_state_round_trip(tmp_path) -> None:
    claim_state_path = tmp_path / "usdc-claim-state.json"
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)
    disposable_wallet = Wallet.create()

    state = support.USDCClaimState(
        canonical_wallet_address="rCanonical",
        issuer="rIssuer",
        last_successful_session_claim_at=now,
        last_prepared_claim_at=now - timedelta(minutes=5),
        claim_wallets=[
            support.USDCClaimWalletState(
                classic_address=disposable_wallet.classic_address,
                seed=disposable_wallet.seed,
                created_at=now - timedelta(minutes=10),
                trustline_create_tx_hash="TRUSTTX",
            )
        ],
    )

    support.write_usdc_claim_state(claim_state_path, state)
    loaded = support.load_usdc_claim_state(claim_state_path, "rCanonical", "rIssuer")

    assert loaded.canonical_wallet_address == "rCanonical"
    assert loaded.issuer == "rIssuer"
    assert loaded.last_successful_session_claim_at == now
    assert loaded.last_prepared_claim_at == now - timedelta(minutes=5)
    assert loaded.claim_wallets[0].classic_address == disposable_wallet.classic_address
    assert loaded.claim_wallets[0].trustline_create_tx_hash == "TRUSTTX"
    assert loaded.claim_wallets[0].status == support.CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING


def test_recover_tracked_usdc_claim_wallets_sweeps_and_deletes_wallet(tmp_path, monkeypatch) -> None:
    claim_state_path = tmp_path / "usdc-claim-state.json"
    accumulator_wallet = Wallet.create()
    disposable_wallet = Wallet.create()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    state = support.USDCClaimState(
        canonical_wallet_address=accumulator_wallet.classic_address,
        issuer="rIssuer",
        claim_wallets=[
            support.USDCClaimWalletState(
                classic_address=disposable_wallet.classic_address,
                seed=disposable_wallet.seed,
                created_at=now - timedelta(hours=1),
                status=support.CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING,
            )
        ],
    )
    support.write_usdc_claim_state(claim_state_path, state)

    trustlines: dict[str, dict[str, str] | None] = {
        accumulator_wallet.classic_address: {"balance": "0", "limit": "100000", "account": "rIssuer", "currency": "USDC"},
        disposable_wallet.classic_address: {"balance": "7.5", "limit": "100000", "account": "rIssuer", "currency": "USDC"},
    }
    xrp_balances = {
        accumulator_wallet.classic_address: 0,
        disposable_wallet.classic_address: 99_999_970,
    }

    monkeypatch.setattr(support, "ensure_usdc_trustline", lambda _client, _wallet, _issuer: None)
    monkeypatch.setattr(
        support,
        "get_validated_account_root",
        lambda _client, address: {
            "account_data": {"Balance": str(xrp_balances[address]), "Sequence": "100"},
            "ledger_index": 400,
        },
    )
    monkeypatch.setattr(
        support,
        "get_validated_trustline",
        lambda _client, address, _issuer, currency_code="RLUSD": trustlines[address],
    )

    def fake_submit_payment(_client, wallet, destination_address, _issuer, amount):
        trustlines[wallet.classic_address] = {"balance": "0", "limit": "100000", "account": "rIssuer", "currency": "USDC"}
        trustlines[destination_address] = {
            "balance": str(Decimal(trustlines[destination_address]["balance"]) + amount),
            "limit": "100000",
            "account": "rIssuer",
            "currency": "USDC",
        }
        return "SWEEPTX"

    monkeypatch.setattr(support, "submit_validated_usdc_payment", fake_submit_payment)
    monkeypatch.setattr(
        support,
        "wait_for_trustline_balance_increase",
        lambda _client, address, _issuer, *, starting_balance, increase, timeout_seconds=30, currency_code="RLUSD": (
            Decimal(trustlines[address]["balance"])
        ),
    )
    monkeypatch.setattr(
        support,
        "reset_usdc_trustline",
        lambda _client, wallet, _issuer: (
            trustlines.__setitem__(
                wallet.classic_address,
                {"balance": "0", "limit": "0", "account": "rIssuer", "currency": "USDC"},
            )
            or "RESETTX"
        ),
    )
    monkeypatch.setattr(
        support,
        "wait_for_trustline_removal",
        lambda _client, address, _issuer, timeout_seconds=30, currency_code="RLUSD": trustlines.__setitem__(address, None) is None,
    )
    monkeypatch.setattr(support, "submit_validated_account_delete", lambda _client, _wallet, _destination: "DELETETX")

    recovered_state, summary = support.recover_tracked_usdc_claim_wallets(
        client=None,
        accumulator_wallet=accumulator_wallet,
        issuer="rIssuer",
        claim_state_file=claim_state_path,
        now=now,
    )

    record = recovered_state.claim_wallets[0]
    assert summary.recovered_usdc_amount == Decimal("7.5")
    assert summary.deleted_wallets == 1
    assert recovered_state.last_successful_session_claim_at == now
    assert record.status == support.CLAIM_WALLET_STATUS_DELETED
    assert record.usdc_sweep_tx_hash == "SWEEPTX"
    assert record.trustline_reset_tx_hash == "RESETTX"
    assert record.account_delete_tx_hash == "DELETETX"
    assert record.last_known_usdc_balance == Decimal("0")
    assert record.last_known_xrp_balance_drops == 0
    assert record.deleted_at == now


def test_prepare_usdc_topup_honors_local_cooldown(tmp_path, monkeypatch) -> None:
    claim_state_path = tmp_path / "usdc-claim-state.json"
    wallets = support.LiveWalletPair(wallet_a=Wallet.create(), wallet_b=Wallet.create())
    previous_claim_at = datetime(2026, 3, 12, 13, 30, tzinfo=timezone.utc)
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        support,
        "recover_tracked_usdc_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.USDCClaimState(
                canonical_wallet_address=wallets.wallet_a.classic_address,
                issuer="rIssuer",
                last_successful_session_claim_at=previous_claim_at,
            ),
            support.USDCClaimRecoverySummary(),
        ),
    )
    monkeypatch.setattr(support, "ensure_usdc_trustline", lambda _client, _wallet, _issuer: None)
    created_wallet = {"value": False}
    monkeypatch.setattr(
        support,
        "generate_faucet_wallet",
        lambda *_args, **_kwargs: created_wallet.__setitem__("value", True),
    )

    result = support.prepare_usdc_topup(
        client=None,
        wallets=wallets,
        issuer="rIssuer",
        now=now,
        claim_state_file=claim_state_path,
    )

    assert result.status == "cooldown"
    assert result.cooldown_until == previous_claim_at + timedelta(hours=2)
    assert created_wallet["value"] is False


def test_prepare_usdc_topup_creates_claim_wallet_and_persists_manual_instructions(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "usdc-claim-state.json"
    wallets = support.LiveWalletPair(wallet_a=Wallet.create(), wallet_b=Wallet.create())
    disposable_wallet = Wallet.create()
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        support,
        "recover_tracked_usdc_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.USDCClaimState(
                canonical_wallet_address=wallets.wallet_a.classic_address,
                issuer="rIssuer",
            ),
            support.USDCClaimRecoverySummary(recovered_usdc_amount=Decimal("2.25")),
        ),
    )

    def fake_ensure(_client, wallet, _issuer, limit_value="100000"):
        if wallet.classic_address == wallets.wallet_a.classic_address:
            return None
        return "TRUSTTX"

    monkeypatch.setattr(support, "ensure_usdc_trustline", fake_ensure)
    monkeypatch.setattr(support, "generate_faucet_wallet", lambda *_args, **_kwargs: disposable_wallet)

    result = support.prepare_usdc_topup(
        client=None,
        wallets=wallets,
        issuer="rIssuer",
        now=now,
        claim_state_file=claim_state_path,
    )

    persisted_state = support.load_usdc_claim_state(
        claim_state_path,
        wallets.wallet_a.classic_address,
        "rIssuer",
    )
    record = persisted_state.claim_wallets[0]

    assert result.status == "awaiting_manual_claim"
    assert result.swept_amount == Decimal("2.25")
    assert result.created_claim_wallet_address == disposable_wallet.classic_address
    assert "faucet.circle.com" in result.message
    assert persisted_state.last_prepared_claim_at == now
    assert record.classic_address == disposable_wallet.classic_address
    assert record.trustline_create_tx_hash == "TRUSTTX"
    assert record.status == support.CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING


def test_prepare_usdc_topup_bridges_accumulator_balance_to_usdc_buyer(
    tmp_path,
    monkeypatch,
) -> None:
    claim_state_path = tmp_path / "usdc-claim-state.json"
    wallet_set = build_demo_wallet_set()
    previous_claim_at = datetime(2026, 3, 12, 13, 30, tzinfo=timezone.utc)
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)
    balances = {
        wallet_set.merchant_wallet.classic_address: Decimal("2.25"),
        wallet_set.buyer_wallet("usdc").classic_address: Decimal("0"),
    }
    ensure_calls: list[str] = []

    monkeypatch.setattr(
        support,
        "recover_tracked_usdc_claim_wallets",
        lambda _client, _accumulator_wallet, _issuer, claim_state_file=None, now=None: (
            support.USDCClaimState(
                canonical_wallet_address=wallet_set.merchant_wallet.classic_address,
                issuer="rIssuer",
                last_successful_session_claim_at=previous_claim_at,
            ),
            support.USDCClaimRecoverySummary(recovered_usdc_amount=Decimal("2.25")),
        ),
    )
    monkeypatch.setattr(
        support,
        "ensure_usdc_trustline",
        lambda _client, wallet, _issuer, limit_value="100000": ensure_calls.append(
            wallet.classic_address
        )
        or None,
    )
    monkeypatch.setattr(
        support,
        "get_validated_usdc_trustline_balance",
        lambda _client, address, _issuer: balances.get(address, Decimal("0")),
    )

    def fake_submit_payment(_client, wallet, destination_address, _issuer, amount):
        balances[wallet.classic_address] -= amount
        balances[destination_address] = balances.get(destination_address, Decimal("0")) + amount
        return "BRIDGETX"

    monkeypatch.setattr(support, "submit_validated_usdc_payment", fake_submit_payment)
    monkeypatch.setattr(
        support,
        "wait_for_trustline_balance_increase",
        lambda _client, _address, _issuer, *, starting_balance, increase, timeout_seconds=30, currency_code=support.USDC_CODE: starting_balance
        + increase,
    )

    result = support.prepare_usdc_topup(
        client=None,
        wallets=wallet_set,
        issuer="rIssuer",
        now=now,
        claim_state_file=claim_state_path,
    )

    assert result.status == "cooldown"
    assert result.buyer_wallet_address == wallet_set.buyer_wallet("usdc").classic_address
    assert result.bridged_amount == Decimal("2.25")
    assert balances[wallet_set.merchant_wallet.classic_address] == Decimal("0")
    assert balances[wallet_set.buyer_wallet("usdc").classic_address] == Decimal("2.25")
    assert ensure_calls == [
        wallet_set.merchant_wallet.classic_address,
        wallet_set.buyer_wallet("usdc").classic_address,
    ]
