"""Pricing tests (spec section 9).

Every price in this file is a synthetic test fixture. The shipped registry
sheets are asserted to hold no prices at all, because Tamias does not invent
prices (AGENTS rule 3).
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from tamias.core.ledger import UNKNOWN, Ledger
from tamias.core.pricing import (
    DOLLAR_LABEL,
    EVIDENCE_OFFICIAL,
    EVIDENCE_UNRESOLVED,
    PRICE_FIELDS,
    PriceRegistry,
    PriceSheetError,
    derive_price_sheet_id,
    load_sheet,
    price_and_store,
    price_request,
    sheet_from_mapping,
    store_priced_record,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRICE_DIR = REPO_ROOT / "registry" / "prices"

# Synthetic fixture prices. Not real vendor prices.
P_IN = Decimal("3.00")
P_OUT = Decimal("15.00")
P_READ = Decimal("0.30")
P_W5 = Decimal("3.75")
P_W1H = Decimal("6.00")


def sheet_data(**overrides: Any) -> dict[str, Any]:
    """A complete, valid section 9.1 sheet. Prices default to synthetic ones."""
    data: dict[str, Any] = {
        "provider": "anthropic",
        "model": "test-model-x",
        "accessed": "2026-01-15",
        "source_url": "https://example.invalid/pricing",
        "input_per_million": float(P_IN),
        "output_per_million": float(P_OUT),
        "cache_read_per_million": float(P_READ),
        "cache_write_5m_per_million": float(P_W5),
        "cache_write_1h_per_million": float(P_W1H),
        "long_context_tiers": [],
        "speed_or_service_tiers": {},
        "credits_or_refunds": {},
    }
    data.update(overrides)
    return data


def make_registry(**overrides: Any) -> PriceRegistry:
    return PriceRegistry([sheet_from_mapping(sheet_data(**overrides))])


def record(**overrides: Any) -> dict[str, Any]:
    """A ledger row with only the token buckets the test names.

    Token buckets are absent by default so that a test can express the
    difference between "exposed as 0" and "not exposed at all", which is what
    spec 9.3 turns on.
    """
    row: dict[str, Any] = {
        "provider": "anthropic",
        "model_id": "test-model-x",
        "speed_or_service_tier": None,
    }
    row.update(overrides)
    return row


def zero_record() -> dict[str, Any]:
    """A row whose every bucket is exposed and zero."""
    return record(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
    )


def combined_registry(*sheets: Any) -> PriceRegistry:
    """A registry holding every dated sheet, the way the price directory does."""
    return PriceRegistry(list(sheets))


# --- 9.1 sheet loading -----------------------------------------------------


def test_template_sheet_declares_every_spec_9_1_field():
    data = yaml.safe_load((PRICE_DIR / "TEMPLATE.yaml").read_text(encoding="utf-8"))
    for field in (
        "provider",
        "model",
        "accessed",
        "source_url",
        *PRICE_FIELDS,
        "long_context_tiers",
        "speed_or_service_tiers",
        "credits_or_refunds",
    ):
        assert field in data, f"TEMPLATE.yaml is missing spec 9.1 field {field}"


def test_no_shipped_price_sheet_contains_a_price():
    """Tamias must not ship invented prices."""
    sheets = [p for p in PRICE_DIR.glob("*.yaml") if p.name.upper() != "TEMPLATE.YAML"]
    for path in sheets:
        sheet = load_sheet(path)
        for field in PRICE_FIELDS:
            assert getattr(sheet, field) is None, (
                f"{path.name}: {field} must stay null until an owner fills it in"
            )


def test_registry_loads_shipped_sheets_without_inventing_prices():
    registry = PriceRegistry.from_directory(PRICE_DIR)
    assert registry.load_errors() == []
    # Only the blank template exists, so no sheet can match any model yet.
    assert registry.sheets() == []


def test_price_sheet_id_is_derived_from_sheet_contents():
    sheet = sheet_from_mapping(sheet_data())
    assert sheet.price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert sheet.price_sheet_id == derive_price_sheet_id(
        "anthropic", "test-model-x", "2026-01-15"
    )


def test_null_sheet_parts_produce_unknown_in_the_id():
    assert sheet_from_mapping(sheet_data(model=None, accessed=None)).price_sheet_id == (
        f"anthropic/{UNKNOWN}@{UNKNOWN}"
    )


def test_sheet_missing_a_spec_field_is_rejected():
    data = sheet_data()
    del data["cache_write_1h_per_million"]
    with pytest.raises(PriceSheetError, match="cache_write_1h_per_million"):
        sheet_from_mapping(data)


def test_sheet_with_an_unknown_key_is_rejected():
    with pytest.raises(PriceSheetError, match="unsupported key"):
        sheet_from_mapping(sheet_data(input_per_1k=1.0))


def test_non_numeric_price_is_an_error_not_a_silent_null():
    with pytest.raises(PriceSheetError, match="is not a number"):
        sheet_from_mapping(sheet_data(input_per_million="about three dollars"))


# --- 9.3 bucket pricing ----------------------------------------------------


def test_input_bucket_is_priced_correctly():
    priced = price_request(record(input_tokens=1_000_000), make_registry())
    assert priced.cost == P_IN
    assert priced.cost_status == "priced"
    item = priced.line_items[0]
    assert (item.label, item.tokens, item.price_per_million) == ("input", 1_000_000, P_IN)


def test_output_bucket_is_priced_correctly():
    priced = price_request(record(output_tokens=500_000), make_registry())
    assert priced.cost == P_OUT / 2


def test_cache_read_bucket_is_priced_correctly():
    priced = price_request(record(cache_read_tokens=2_000_000), make_registry())
    assert priced.cost == P_READ * 2


def test_cache_write_5m_bucket_is_priced_correctly():
    priced = price_request(record(cache_write_5m_tokens=1_000_000), make_registry())
    assert priced.cost == P_W5


def test_cache_write_1h_bucket_is_priced_correctly():
    priced = price_request(record(cache_write_1h_tokens=1_000_000), make_registry())
    assert priced.cost == P_W1H


def test_5m_and_1h_cache_writes_are_priced_separately():
    priced = price_request(
        record(cache_write_5m_tokens=1_000_000, cache_write_1h_tokens=1_000_000),
        make_registry(),
    )
    assert priced.cost == P_W5 + P_W1H
    labels = {item.label: item.price_per_million for item in priced.line_items}
    assert labels["cache_write_5m"] == P_W5
    assert labels["cache_write_1h"] == P_W1H


def test_all_buckets_combined_price_as_the_sum_of_the_parts():
    row = record(
        input_tokens=1000,
        output_tokens=2000,
        cache_read_tokens=3000,
        cache_write_5m_tokens=4000,
        cache_write_1h_tokens=5000,
    )
    priced = price_request(row, make_registry())
    expected = (
        Decimal(1000) * P_IN
        + Decimal(2000) * P_OUT
        + Decimal(3000) * P_READ
        + Decimal(4000) * P_W5
        + Decimal(5000) * P_W1H
    ) / Decimal(10) ** 6
    assert priced.cost == expected.normalize()


def test_zero_token_buckets_contribute_zero_without_requiring_a_price():
    registry = make_registry(
        input_per_million=None,
        output_per_million=None,
        cache_read_per_million=None,
        cache_write_5m_per_million=None,
        cache_write_1h_per_million=None,
    )
    priced = price_request(record(output_tokens=0), registry)
    assert priced.cost == Decimal(0)
    assert priced.cost_status == "priced"


def test_zero_usage_record_prices_to_zero():
    priced = price_request(zero_record(), make_registry())
    assert priced.cost == Decimal(0)
    assert priced.cost_status == "priced"


# --- UNKNOWN, never zero ---------------------------------------------------


@pytest.mark.parametrize(
    ("missing_field", "row"),
    [
        ("input_per_million", record(input_tokens=10)),
        ("output_per_million", record(output_tokens=10)),
        ("cache_read_per_million", record(cache_read_tokens=10)),
        ("cache_write_5m_per_million", record(cache_write_5m_tokens=10)),
        ("cache_write_1h_per_million", record(cache_write_1h_tokens=10)),
    ],
)
def test_missing_price_yields_unknown_not_zero(missing_field, row):
    registry = make_registry(**{missing_field: None})
    priced = price_request(row, registry)
    assert priced.cost == UNKNOWN
    assert priced.is_unknown
    assert priced.unknown_reason == f"price_missing:{missing_field}"


def test_unknown_cost_is_not_zero():
    priced = price_request(record(input_tokens=500), make_registry(input_per_million=None))
    assert priced.cost != Decimal(0)
    assert str(priced.cost) == UNKNOWN
    assert priced.evidence_status == EVIDENCE_UNRESOLVED


def test_missing_model_is_unknown():
    priced = price_request(record(model_id="unknown"), make_registry())
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "model_not_identified"


def test_absent_model_field_is_unknown():
    row = record()
    row.pop("model_id")
    priced = price_request(row, make_registry())
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "model_not_identified"


def test_no_sheet_for_model_is_unknown():
    priced = price_request(record(model_id="other-model"), make_registry())
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "no_price_sheet_for_model"


def test_unknown_price_sheet_id_is_reported():
    priced = price_request(record(input_tokens=5), make_registry(), price_sheet_id="nope/1@x")
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "price_sheet_not_found"


def test_ambiguous_sheets_are_unknown_rather_than_guessed():
    registry = PriceRegistry(
        [
            sheet_from_mapping(sheet_data()),
            sheet_from_mapping(sheet_data(accessed="2026-06-01")),
        ]
    )
    priced = price_request(record(input_tokens=5), registry)
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "ambiguous_price_sheet"
    # Naming the sheet resolves the ambiguity explicitly.
    named = price_request(
        record(input_tokens=1_000_000), registry, price_sheet_id="anthropic/test-model-x@2026-06-01"
    )
    assert named.cost == P_IN


def test_unsplit_cache_write_total_is_unknown():
    """9.3 prices exposed 5m/1h fields; a combined total identifies no bucket."""
    priced = price_request(record(cache_write_tokens=1000), make_registry())
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "cache_write_basis_unknown"


def test_unsplit_total_with_no_split_prices_still_unknown():
    registry = make_registry(cache_write_5m_per_million=None, cache_write_1h_per_million=None)
    priced = price_request(record(cache_write_tokens=1000), registry)
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "cache_write_basis_unknown"


def test_record_without_any_token_field_is_unknown():
    priced = price_request({"provider": "anthropic", "model_id": "test-model-x"}, make_registry())
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "no_token_fields"


def test_listed_service_tier_without_prices_is_unknown():
    registry = make_registry(speed_or_service_tiers={"priority": {}})
    priced = price_request(record(input_tokens=5, speed_or_service_tier="priority"), registry)
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "service_tier_prices_unknown:priority"


def test_service_tier_prices_override_base_prices():
    registry = make_registry(
        speed_or_service_tiers={"priority": {"input_per_million": 6.0}}
    )
    priced = price_request(record(input_tokens=1_000_000, speed_or_service_tier="priority"), registry)
    assert priced.cost == Decimal("6.00")
    assert "speed_tier=priority" in priced.basis


def test_long_context_tier_applies_only_with_a_known_context_length():
    registry = make_registry(
        long_context_tiers=[{"name": "big", "min_context_tokens": 128000, "input_per_million": 6.0}]
    )
    stated = price_request(
        record(input_tokens=1_000_000), registry, context_tokens=200_000
    )
    assert stated.cost == Decimal("6.00")
    assert "long_context_tier=big" in stated.basis

    unstated = price_request(record(input_tokens=1_000_000), registry)
    assert unstated.cost == P_IN
    assert "context_length_unknown" in unstated.basis


# --- 9.2 historical repricing ---------------------------------------------


def test_original_and_analysis_sheet_ids_are_reported_on_first_pricing():
    priced = price_request(record(input_tokens=1_000_000), make_registry())
    assert priced.original_price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert priced.analysis_price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert priced.repriced is False
    assert priced.reprice_blocked is False


def test_historical_record_is_not_silently_repriced():
    old_sheet = sheet_from_mapping(sheet_data())
    new_sheet = sheet_from_mapping(sheet_data(accessed="2026-06-01"))
    registry = combined_registry(old_sheet, new_sheet)

    first = price_request(record(input_tokens=1_000_000), combined_registry(old_sheet))
    assert first.cost == P_IN

    later = price_request(
        record(input_tokens=1_000_000),
        registry,
        price_sheet_id=new_sheet.price_sheet_id,
        original_price_sheet_id=first.original_price_sheet_id,
    )
    # The original sheet still prices the record.
    assert later.cost == P_IN
    assert later.repriced is False
    assert later.reprice_blocked is True
    assert later.price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert later.analysis_price_sheet_id == "anthropic/test-model-x@2026-06-01"


def test_repricing_requires_explicit_opt_in_and_keeps_both_ids():
    old_sheet = sheet_from_mapping(sheet_data())
    new_sheet = sheet_from_mapping(
        sheet_data(accessed="2026-06-01", input_per_million=6.0)
    )
    registry = combined_registry(old_sheet, new_sheet)

    first = price_request(record(input_tokens=1_000_000), combined_registry(old_sheet))
    later = price_request(
        record(input_tokens=1_000_000),
        registry,
        price_sheet_id=new_sheet.price_sheet_id,
        original_price_sheet_id=first.original_price_sheet_id,
        allow_repricing=True,
    )
    assert later.cost == Decimal("6.00")
    assert later.repriced is True
    assert later.original_price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert later.analysis_price_sheet_id == "anthropic/test-model-x@2026-06-01"


def test_missing_original_sheet_never_falls_back_to_the_current_sheet():
    registry = make_registry()
    priced = price_request(
        record(input_tokens=1_000_000),
        registry,
        original_price_sheet_id="anthropic/test-model-x@2025-01-01",
    )
    assert priced.cost == UNKNOWN
    assert priced.unknown_reason == "original_price_sheet_missing"
    assert priced.reprice_blocked is True


# --- receipt obligations (AGENTS rule 5, spec 5.6) -------------------------


def test_every_dollar_output_carries_tokens_sheet_formula_and_evidence():
    priced = price_request(
        record(input_tokens=1000, output_tokens=2000, cache_write_1h_tokens=4000),
        make_registry(),
    )
    assert priced.source_tokens == {
        "input_tokens": 1000,
        "output_tokens": 2000,
        "cache_write_1h_tokens": 4000,
    }
    assert priced.price_sheet_id == "anthropic/test-model-x@2026-01-15"
    assert priced.formula.startswith("C_realized_request = ")
    assert "input_tokens=1000" in priced.formula
    assert priced.evidence_status == EVIDENCE_OFFICIAL
    assert priced.dollar_label == DOLLAR_LABEL


def test_unknown_receipt_still_carries_tokens_formula_and_sheet():
    priced = price_request(
        record(input_tokens=1000, output_tokens=10), make_registry(output_per_million=None)
    )
    text = priced.as_text()
    assert UNKNOWN in text
    assert "input_tokens=1000" in text
    assert "anthropic/test-model-x@2026-01-15" in text
    assert DOLLAR_LABEL in text
    assert priced.source_tokens["input_tokens"] == 1000


def test_evidence_is_unresolved_without_a_source_or_accessed_date():
    priced = price_request(
        record(input_tokens=1_000_000), make_registry(source_url=None)
    )
    assert priced.cost == P_IN
    assert priced.evidence_status == EVIDENCE_UNRESOLVED


def test_as_text_labels_dollars_as_api_equivalent_estimate():
    priced = price_request(record(input_tokens=1_000_000), make_registry())
    text = priced.as_text()
    assert "API-equivalent list-price estimate, not a subscription bill" in text
    assert f"${P_IN}" in text
    assert "quality: not assessed" not in text


def test_credits_are_surfaced_but_never_applied():
    registry = make_registry(credits_or_refunds={"promo_credit_usd": 5.0})
    priced = price_request(record(input_tokens=1_000_000), registry)
    assert priced.cost == P_IN  # credit did not change the token price
    assert "promo_credit_usd" in priced.as_text()
    assert "not applied" in priced.as_text()


# --- persistence -----------------------------------------------------------


def _priced_store() -> tuple[Ledger, int]:
    store = Ledger(":memory:").connect().initialize()
    scan_id = store.begin_scan(
        parser_version="test",
        schema_status="unverified",
        started_utc="2026-01-15T00:00:00+00:00",
        log_root=None,
        schema_id="test",
    )
    row = {name: None for name in __import__(
        "tamias.core.ledger", fromlist=["REQUEST_RECORD_FIELDS"]
    ).REQUEST_RECORD_FIELDS}
    row.update(
        raw_record_hash="a" * 64,
        parser_version="test",
        provider="anthropic",
        model_id="test-model-x",
        input_tokens=1_000_000,
    )
    outcome = store.insert_record(row, scan_id=scan_id, source_file="a.jsonl", source_line=1)
    return store, int(outcome.record_uid)


def test_priced_record_stores_both_sheet_ids_and_the_amount():
    store, uid = _priced_store()
    registry = make_registry()
    priced = price_and_store(store.conn, dict(store.rows()[0]), registry, record_uid=uid)
    row = store.conn.execute("SELECT * FROM priced_record WHERE record_uid = ?", (uid,)).fetchone()
    assert row["cost_status"] == "priced"
    assert Decimal(row["cost_usd_exact"]) == P_IN
    assert row["original_price_sheet_id"] == "anthropic/test-model-x@2026-01-15"
    assert row["analysis_price_sheet_id"] == "anthropic/test-model-x@2026-01-15"
    assert row["dollar_label"] == DOLLAR_LABEL
    assert json.loads(row["source_tokens_json"])["input_tokens"] == 1_000_000
    assert "C_realized_request" in row["formula"]
    assert priced.cost == P_IN


def test_unknown_price_is_stored_as_null_cost_with_a_reason():
    store, uid = _priced_store()
    priced = price_and_store(
        store.conn,
        dict(store.rows()[0]),
        make_registry(input_per_million=None),
        record_uid=uid,
    )
    row = store.conn.execute("SELECT * FROM priced_record WHERE record_uid = ?", (uid,)).fetchone()
    assert row["cost_status"] == "unknown"
    assert row["cost_usd"] is None
    assert row["cost_usd_exact"] is None
    assert row["unknown_reason"] == "price_missing:input_per_million"
    assert priced.cost == UNKNOWN


def test_second_pass_over_the_same_row_keeps_the_original_sheet():
    store, uid = _priced_store()
    old_sheet = sheet_from_mapping(sheet_data())
    price_and_store(store.conn, dict(store.rows()[0]), combined_registry(old_sheet), record_uid=uid)

    new_sheet = sheet_from_mapping(sheet_data(accessed="2026-06-01", input_per_million=6.0))
    newer = combined_registry(old_sheet, new_sheet)
    again = price_and_store(
        store.conn,
        dict(store.rows()[0]),
        newer,
        record_uid=uid,
        price_sheet_id=new_sheet.price_sheet_id,
    )
    assert again.cost == P_IN
    row = store.conn.execute("SELECT * FROM priced_record WHERE record_uid = ?", (uid,)).fetchone()
    assert row["original_price_sheet_id"] == "anthropic/test-model-x@2026-01-15"
    assert row["analysis_price_sheet_id"] == "anthropic/test-model-x@2026-06-01"
    assert row["reprice_blocked"] == 1
    assert row["repriced"] == 0
    assert Decimal(row["cost_usd_exact"]) == P_IN


def test_opted_in_repricing_is_recorded_as_such():
    store, uid = _priced_store()
    old_sheet = sheet_from_mapping(sheet_data())
    price_and_store(store.conn, dict(store.rows()[0]), combined_registry(old_sheet), record_uid=uid)
    new_sheet = sheet_from_mapping(sheet_data(accessed="2026-06-01", input_per_million=6.0))
    later = price_and_store(
        store.conn,
        dict(store.rows()[0]),
        combined_registry(old_sheet, new_sheet),
        record_uid=uid,
        price_sheet_id=new_sheet.price_sheet_id,
        allow_repricing=True,
    )
    assert later.cost == Decimal("6.00")
    row = store.conn.execute("SELECT * FROM priced_record WHERE record_uid = ?", (uid,)).fetchone()
    assert row["repriced"] == 1
    assert row["original_price_sheet_id"] == "anthropic/test-model-x@2026-01-15"
    assert row["analysis_price_sheet_id"] == "anthropic/test-model-x@2026-06-01"
    assert Decimal(row["cost_usd_exact"]) == Decimal("6.00")


def test_priced_table_does_not_mutate_the_observed_request_record():
    store, uid = _priced_store()
    before = dict(store.rows()[0])
    price_and_store(store.conn, dict(before), make_registry(), record_uid=uid)
    after = dict(store.rows()[0])
    assert before == after
    assert "price_sheet_id" not in after
    assert "cost_usd" not in after


def test_store_priced_record_requires_a_price_sheet_id_field_to_exist():
    store, uid = _priced_store()
    priced = price_request(dict(store.rows()[0]), make_registry())
    store_priced_record(store.conn, uid, priced, priced_utc="2026-01-15T00:00:00+00:00")
    columns = {
        d[0]
        for d in store.conn.execute("SELECT * FROM priced_record LIMIT 0").description
    }
    assert {"original_price_sheet_id", "analysis_price_sheet_id"} <= columns