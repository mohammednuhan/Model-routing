"""Dated price sheets and per-request pricing (spec section 9).

Rules this module enforces:

* 9.1 -- every sheet carries provider, model, accessed, source_url, the five
  per-million prices, long_context_tiers, speed_or_service_tiers and
  credits_or_refunds. A sheet missing any of those keys is rejected instead of
  being read as a null price.
* 9.2 -- ``original_price_sheet_id`` and ``analysis_price_sheet_id`` are
  stored on every priced record. A row that was already priced keeps its
  original sheet; re-pricing it under another sheet requires an explicit
  opt-in and retains both ids and both amounts.
* 9.3 -- when 5m and 1h cache-write token fields are exposed they are priced
  separately at their own prices. An unsplit write total is UNKNOWN, because
  the applicable bucket is not identifiable from the record.
* 9.4 -- ``C_realized_request`` (actual billed request cost) is computed here.
  ``E_cache`` is an exposure estimate from a reconstructed prefix and is NOT
  computed by this module; it is not the same quantity.
* AGENTS rule 3 -- if any price a request needs is null, the cost is the string
  ``UNKNOWN``. It is never zero and never partially zero.
* AGENTS rule 5 -- every dollar output carries source tokens, price sheet id,
  formula and evidence status, and is labelled an API-equivalent list-price
  estimate rather than a subscription bill.
* AGENTS rule 4 -- nothing here is a causal quantity. ``C_realized_request`` is
  an observed-billing-basis arithmetic result, not the effect of a switch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .ledger import UNKNOWN

# Spec 5.6: all dollar values in Tamias are API-equivalent list-price
# estimates, not subscription bills.
DOLLAR_LABEL = "API-equivalent list-price estimate, not a subscription bill"

# The five price fields of spec 9.1, in the order they are priced.
PRICE_FIELDS: tuple[str, ...] = (
    "input_per_million",
    "output_per_million",
    "cache_read_per_million",
    "cache_write_5m_per_million",
    "cache_write_1h_per_million",
)

SHEET_FIELDS: tuple[str, ...] = (
    "provider",
    "model",
    "accessed",
    "source_url",
    *PRICE_FIELDS,
    "long_context_tiers",
    "speed_or_service_tiers",
    "credits_or_refunds",
)

# Spec P4 evidence vocabulary, restricted to the two values section 9 can
# honestly produce from a price sheet.
EVIDENCE_OFFICIAL = "official_documentation"
EVIDENCE_UNRESOLVED = "unresolved"

# Record token field -> sheet price field. ``cache_write_tokens`` is
# deliberately absent: it is a combined total, not a bucket (see 9.3).
_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("input_tokens", "input_per_million", "input"),
    ("output_tokens", "output_per_million", "output"),
    ("cache_read_tokens", "cache_read_per_million", "cache_read"),
    ("cache_write_5m_tokens", "cache_write_5m_per_million", "cache_write_5m"),
    ("cache_write_1h_tokens", "cache_write_1h_per_million", "cache_write_1h"),
)

_MILLION = Decimal(10) ** 6

# Per-token prices make very small amounts, so amounts are carried at
# sub-nanodollar precision instead of being rounded to cents.
_CENTS_UNIT = Decimal("0.000000000001")


def format_usd(amount: Decimal) -> str:
    """Render a dollar amount without scientific notation and with cents.

    ``Decimal.normalize()`` would turn 3.000000 into 3E+0, so amounts are
    formatted explicitly here. Receipts must be readable and exact.
    """
    quantized = amount.quantize(_CENTS_UNIT)
    text = f"{quantized:.12f}".rstrip("0")
    if text.endswith("."):
        text += "00"
    elif len(text.split(".")[1]) == 1:
        text += "0"
    return f"${text}"


class PriceSheetError(ValueError):
    """A sheet is missing required 9.1 fields or holds an unusable value."""


def derive_price_sheet_id(provider: Any, model: Any, accessed: Any) -> str:
    """Deterministic sheet id: ``<provider>/<model>@<accessed>``.

    Derived rather than declared so an id can never drift from the sheet it
    names. Missing parts become UNKNOWN rather than being omitted.
    """

    def part(value: Any) -> str:
        if value is None:
            return UNKNOWN
        text = str(value).strip()
        return text if text else UNKNOWN

    return f"{part(provider)}/{part(model)}@{part(accessed)}"


@dataclass(frozen=True)
class PriceSheet:
    """One dated price sheet."""

    provider: str | None
    model: str | None
    accessed: str | None
    source_url: str | None
    input_per_million: Decimal | None
    output_per_million: Decimal | None
    cache_read_per_million: Decimal | None
    cache_write_5m_per_million: Decimal | None
    cache_write_1h_per_million: Decimal | None
    long_context_tiers: tuple[Mapping[str, Any], ...] = ()
    speed_or_service_tiers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    credits_or_refunds: Mapping[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    @property
    def price_sheet_id(self) -> str:
        return derive_price_sheet_id(self.provider, self.model, self.accessed)

    def has_source(self) -> bool:
        return bool(self.accessed and self.source_url)

    def prices(self) -> dict[str, Decimal | None]:
        return {name: getattr(self, name) for name in PRICE_FIELDS}


@dataclass(frozen=True)
class LineItem:
    """One priced token bucket with the tokens it came from."""

    label: str
    tokens: int
    tokens_field: str
    price_field: str
    price_per_million: Decimal | None
    cost: Decimal | None

    def formula(self) -> str:
        if self.tokens == 0:
            return f"{self.tokens_field}=0 -> $0 (no price needed)"
        if self.price_per_million is None:
            return f"{self.tokens_field}={self.tokens} -> UNKNOWN (missing {self.price_field})"
        return (
            f"{self.tokens_field}={self.tokens} * {format_usd(self.price_per_million)}/1e6"
        )


@dataclass(frozen=True)
class PricedRequest:
    """The priced result for one request record.

    ``cost`` is ``C_realized_request``: a ``Decimal`` dollar amount, or the
    string ``UNKNOWN``. It is never ``Decimal(0)`` when a price was missing.
    """

    cost: Decimal | str
    cost_status: str  # priced | unknown
    unknown_reason: str | None
    price_sheet_id: str | None
    original_price_sheet_id: str | None
    analysis_price_sheet_id: str | None
    repriced: bool
    reprice_blocked: bool
    formula: str
    evidence_status: str
    source_tokens: dict[str, int]
    line_items: tuple[LineItem, ...]
    credits_or_refunds: Mapping[str, Any] = field(default_factory=dict)
    basis: str = "base"
    dollar_label: str = DOLLAR_LABEL

    @property
    def is_unknown(self) -> bool:
        return self.cost_status == "unknown"

    def as_text(self) -> str:
        """Human-readable receipt. Raw tokens sit beside the dollar figure."""
        lines = [
            "PRICED REQUEST",
            "  C_realized_request        : "
            + (str(self.cost) if self.is_unknown else format_usd(self.cost)),
            f"  dollar basis              : {self.dollar_label}",
            f"  price sheet               : {self.price_sheet_id or UNKNOWN}",
            f"  original_price_sheet_id   : {self.original_price_sheet_id or UNKNOWN}",
            f"  analysis_price_sheet_id   : {self.analysis_price_sheet_id or UNKNOWN}",
            f"  repriced                  : {self.repriced}",
            f"  pricing basis             : {self.basis}",
            f"  evidence status           : {self.evidence_status}",
        ]
        if self.unknown_reason:
            lines.append(f"  unknown reason            : {self.unknown_reason}")
        if self.reprice_blocked:
            lines.append(
                "  reprice blocked           : original price sheet retained; "
                "a different sheet was requested but not applied"
            )
        lines.append("  source tokens / formula")
        for item in self.line_items:
            amount = "UNKNOWN" if item.cost is None else format_usd(item.cost)
            lines.append(f"    {item.label:<16} {item.formula()} = {amount}")
        lines.append(f"  formula                   : {self.formula}")
        if self.credits_or_refunds:
            lines.append("  credits_or_refunds        :")
            for key in sorted(self.credits_or_refunds):
                lines.append(f"    {key}: {self.credits_or_refunds[key]} (surfaced, not applied)")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost": UNKNOWN if self.is_unknown else str(self.cost),
            "cost_status": self.cost_status,
            "unknown_reason": self.unknown_reason,
            "price_sheet_id": self.price_sheet_id,
            "original_price_sheet_id": self.original_price_sheet_id,
            "analysis_price_sheet_id": self.analysis_price_sheet_id,
            "repriced": self.repriced,
            "reprice_blocked": self.reprice_blocked,
            "formula": self.formula,
            "evidence_status": self.evidence_status,
            "source_tokens": dict(self.source_tokens),
            "line_items": [
                {
                    "label": item.label,
                    "tokens_field": item.tokens_field,
                    "tokens": item.tokens,
                    "price_field": item.price_field,
                    "price_per_million": (
                        None if item.price_per_million is None else str(item.price_per_million)
                    ),
                    "cost": None if item.cost is None else str(item.cost),
                }
                for item in self.line_items
            ],
            "credits_or_refunds": dict(self.credits_or_refunds),
            "basis": self.basis,
            "dollar_label": self.dollar_label,
        }


def _to_price(value: Any, *, field_name: str, path: str) -> Decimal | None:
    """Coerce a YAML price to Decimal. Anything unparseable is an error."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise PriceSheetError(f"{path}: {field_name} must be a number or null, not a bool")
    if isinstance(value, (int, float, str)):
        try:
            price = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise PriceSheetError(
                f"{path}: {field_name}={value!r} is not a number"
            ) from exc
        if price < 0:
            raise PriceSheetError(f"{path}: {field_name} must not be negative")
        return price
    raise PriceSheetError(f"{path}: {field_name} must be a number or null")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        raise PriceSheetError(f"expected a string or null, got {value!r}")
    text = str(value).strip()
    return text or None


def sheet_from_mapping(data: Mapping[str, Any], *, path: str = "<memory>") -> PriceSheet:
    """Build a sheet from parsed YAML, enforcing the 9.1 field list."""
    if not isinstance(data, Mapping):
        raise PriceSheetError(f"{path}: sheet must be a YAML mapping")
    missing = [name for name in SHEET_FIELDS if name not in data]
    if missing:
        raise PriceSheetError(f"{path}: missing spec 9.1 field(s): {', '.join(missing)}")

    unknown_keys = [name for name in data if name not in SHEET_FIELDS]
    if unknown_keys:
        raise PriceSheetError(
            f"{path}: unsupported key(s) {', '.join(sorted(unknown_keys))}; "
            f"a sheet may only carry {', '.join(SHEET_FIELDS)}"
        )

    tiers_raw = data["long_context_tiers"]
    if tiers_raw is None:
        tiers_raw = []
    if not isinstance(tiers_raw, Sequence) or isinstance(tiers_raw, (str, bytes)):
        raise PriceSheetError(f"{path}: long_context_tiers must be a list")
    tiers: list[Mapping[str, Any]] = []
    for entry in tiers_raw:
        if not isinstance(entry, Mapping):
            raise PriceSheetError(f"{path}: long_context_tiers entries must be mappings")
        tiers.append(dict(entry))

    speed_raw = data["speed_or_service_tiers"] or {}
    if not isinstance(speed_raw, Mapping):
        raise PriceSheetError(f"{path}: speed_or_service_tiers must be a mapping")
    speed: dict[str, Mapping[str, Any]] = {}
    for name, entry in speed_raw.items():
        if not isinstance(entry, Mapping):
            raise PriceSheetError(
                f"{path}: speed_or_service_tiers.{name} must be a mapping"
            )
        speed[str(name)] = dict(entry)

    credits_raw = data["credits_or_refunds"] or {}
    if not isinstance(credits_raw, Mapping):
        raise PriceSheetError(f"{path}: credits_or_refunds must be a mapping")

    prices = {
        name: _to_price(data[name], field_name=name, path=path) for name in PRICE_FIELDS
    }
    return PriceSheet(
        provider=_optional_str(data["provider"]),
        model=_optional_str(data["model"]),
        accessed=_optional_str(data["accessed"]),
        source_url=_optional_str(data["source_url"]),
        long_context_tiers=tuple(tiers),
        speed_or_service_tiers=speed,
        credits_or_refunds=dict(credits_raw),
        source_path=str(path),
        **prices,
    )


def load_sheet(path: str | Path) -> PriceSheet:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        raise PriceSheetError(f"{path}: sheet is empty")
    return sheet_from_mapping(data, path=str(path))


class PriceRegistry:
    """The set of dated price sheets found on disk."""

    def __init__(self, sheets: Iterable[PriceSheet] = ()) -> None:
        self._sheets: list[PriceSheet] = list(sheets)

    def __len__(self) -> int:
        return len(self._sheets)

    def __iter__(self):
        return iter(self._sheets)

    def sheets(self) -> list[PriceSheet]:
        return list(self._sheets)

    def add(self, sheet: PriceSheet) -> None:
        self._sheets.append(sheet)

    @classmethod
    def from_directory(cls, root: str | Path = "registry/prices") -> "PriceRegistry":
        """Load every ``*.yaml`` sheet under ``root``.

        A sheet that fails validation is skipped, not guessed at: it is left out
        of the registry so the affected requests report UNKNOWN with the sheet
        problem visible rather than being priced from a half-read file.
        """
        root = Path(root)
        registry = cls()
        if not root.exists():
            return registry
        for path in sorted(root.rglob("*.yaml")):
            if path.name.upper() == "TEMPLATE.YAML":
                # The blank template is a schema reference, not a price.
                continue
            registry.add(load_sheet(path))
        return registry

    def load_errors(self, root: str | Path = "registry/prices") -> list[str]:
        """Validation messages for sheets that could not be loaded."""
        root = Path(root)
        problems: list[str] = []
        if not root.exists():
            return problems
        for path in sorted(root.rglob("*.yaml")):
            if path.name.upper() == "TEMPLATE.YAML":
                continue
            try:
                load_sheet(path)
            except PriceSheetError as exc:
                problems.append(str(exc))
            except (OSError, yaml.YAMLError) as exc:
                problems.append(f"{path}: unreadable ({exc.__class__.__name__})")
        return problems

    def get(self, price_sheet_id: str | None) -> PriceSheet | None:
        if not price_sheet_id:
            return None
        for sheet in self._sheets:
            if sheet.price_sheet_id == price_sheet_id:
                return sheet
        return None

    def matches(self, provider: str | None, model: str | None) -> list[PriceSheet]:
        """Sheets whose provider and model match, exactly but case-insensitively."""
        if not provider or not model:
            return []
        p = provider.strip().lower()
        m = model.strip().lower()
        return [
            sheet
            for sheet in self._sheets
            if (sheet.provider or "").strip().lower() == p
            and (sheet.model or "").strip().lower() == m
        ]


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _merge_tier_prices(
    sheet: PriceSheet, *, tier: str | None, context_tokens: int | None
) -> tuple[dict[str, Decimal | None], str, str | None]:
    """Resolve the applicable prices for one request.

    Returns ``(prices, basis, problem)``. A problem means the applicable prices
    cannot be established; the caller must then report UNKNOWN rather than
    falling back to base prices.
    """
    prices = sheet.prices()
    notes: list[str] = ["base"]

    if tier:
        entry = sheet.speed_or_service_tiers.get(tier)
        if entry is None:
            return prices, "base", f"service_tier_not_in_sheet:{tier}"
        if not any(name in entry for name in PRICE_FIELDS):
            # The provider has a distinct tier but the sheet declares no price
            # for it, so the applicable price is not establishable.
            return prices, "base", f"service_tier_prices_unknown:{tier}"
        for name in PRICE_FIELDS:
            if name in entry:
                prices[name] = _to_price(
                    entry[name], field_name=name, path=f"{sheet.price_sheet_id}:{tier}"
                )
        notes.append(f"speed_tier={tier}")

    if sheet.long_context_tiers:
        if context_tokens is None:
            # Tamias does not infer context length from token sums, so a tiered
            # sheet stays on base prices unless the caller states the length.
            notes.append("long_context_tier=not_selected(context_length_unknown)")
        else:
            best: Mapping[str, Any] | None = None
            best_min = -1
            for entry in sheet.long_context_tiers:
                minimum = _as_int(entry.get("min_context_tokens"))
                if minimum is None or minimum > context_tokens:
                    continue
                if minimum > best_min:
                    best, best_min = entry, minimum
            if best is not None:
                for name in PRICE_FIELDS:
                    if name in best:
                        prices[name] = _to_price(
                            best[name],
                            field_name=name,
                            path=f"{sheet.price_sheet_id}:{best.get('name', 'tier')}",
                        )
                notes.append(
                    f"long_context_tier={best.get('name', best_min)}"
                )

    return prices, "+".join(notes), None


def _unknown(
    reason: str,
    *,
    price_sheet_id: str | None,
    original_price_sheet_id: str | None,
    analysis_price_sheet_id: str | None,
    source_tokens: Mapping[str, int],
    line_items: Sequence[LineItem] = (),
    evidence_status: str = EVIDENCE_UNRESOLVED,
    basis: str = "base",
    repriced: bool = False,
    reprice_blocked: bool = False,
    credits: Mapping[str, Any] | None = None,
) -> PricedRequest:
    return PricedRequest(
        cost=UNKNOWN,
        cost_status="unknown",
        unknown_reason=reason,
        price_sheet_id=price_sheet_id,
        original_price_sheet_id=original_price_sheet_id,
        analysis_price_sheet_id=analysis_price_sheet_id,
        repriced=repriced,
        reprice_blocked=reprice_blocked,
        formula=(
            f"C_realized_request = UNKNOWN ({reason}"
            + (f"; sheet={price_sheet_id}" if price_sheet_id else "")
            + ")"
        ),
        evidence_status=evidence_status,
        source_tokens=dict(source_tokens),
        line_items=tuple(line_items),
        credits_or_refunds=dict(credits or {}),
        basis=basis,
    )


def price_request(
    record: Mapping[str, Any],
    registry: PriceRegistry,
    *,
    price_sheet_id: str | None = None,
    original_price_sheet_id: str | None = None,
    allow_repricing: bool = False,
    context_tokens: int | None = None,
) -> PricedRequest:
    """Compute ``C_realized_request`` for one ledger record.

    Spec 9.2: when ``original_price_sheet_id`` is supplied and a different
    sheet is requested, the original sheet still prices the record unless
    ``allow_repricing=True``. Both ids are always reported, so a re-analysis is
    visible rather than silent.
    """
    tokens = {
        tokens_field: value
        for tokens_field, _, _ in _BUCKETS
        if (value := _as_int(record.get(tokens_field))) is not None
    }
    source_tokens = dict(tokens)
    split_write_present = (
        "cache_write_5m_tokens" in tokens or "cache_write_1h_tokens" in tokens
    )
    write_total = _as_int(record.get("cache_write_tokens"))
    if write_total is not None:
        source_tokens["cache_write_tokens"] = write_total

    analysis_sheet: PriceSheet | None = None
    if price_sheet_id:
        analysis_sheet = registry.get(price_sheet_id)
        if analysis_sheet is None:
            return _unknown(
                "price_sheet_not_found",
                price_sheet_id=price_sheet_id,
                original_price_sheet_id=original_price_sheet_id,
                analysis_price_sheet_id=price_sheet_id,
                source_tokens=source_tokens,
            )
    else:
        provider = record.get("provider")
        model = record.get("model_id")
        provider_text = provider.strip() if isinstance(provider, str) else None
        model_text = model.strip() if isinstance(model, str) else None
        if not provider_text or not model_text or "unknown" in (
            provider_text.lower(),
            model_text.lower(),
        ):
            return _unknown(
                "model_not_identified",
                price_sheet_id=None,
                original_price_sheet_id=original_price_sheet_id,
                analysis_price_sheet_id=None,
                source_tokens=source_tokens,
            )
        candidates = registry.matches(provider_text, model_text)
        if not candidates:
            return _unknown(
                "no_price_sheet_for_model",
                price_sheet_id=None,
                original_price_sheet_id=original_price_sheet_id,
                analysis_price_sheet_id=None,
                source_tokens=source_tokens,
            )
        if len(candidates) > 1:
            return _unknown(
                "ambiguous_price_sheet",
                price_sheet_id=None,
                original_price_sheet_id=original_price_sheet_id,
                analysis_price_sheet_id=None,
                source_tokens=source_tokens,
            )
        analysis_sheet = candidates[0]

    sheet = analysis_sheet
    repriced = False
    reprice_blocked = False

    if original_price_sheet_id:
        original_sheet = registry.get(original_price_sheet_id)
        if original_sheet is None:
            return _unknown(
                "original_price_sheet_missing",
                price_sheet_id=analysis_sheet.price_sheet_id,
                original_price_sheet_id=original_price_sheet_id,
                analysis_price_sheet_id=analysis_sheet.price_sheet_id,
                source_tokens=source_tokens,
                repriced=False,
                reprice_blocked=True,
            )
        if analysis_sheet.price_sheet_id != original_price_sheet_id:
            if allow_repricing:
                # Explicit re-analysis: both ids and both amounts are kept.
                repriced = True
            else:
                sheet = original_sheet
                reprice_blocked = True
        else:
            sheet = original_sheet

    if not split_write_present and write_total:
        # 9.3 only defines separate pricing for exposed 5m/1h fields. A combined
        # total does not identify the bucket, so the cost stays UNKNOWN rather
        # than assuming a 5m basis.
        return _unknown(
            "cache_write_basis_unknown",
            price_sheet_id=sheet.price_sheet_id,
            original_price_sheet_id=original_price_sheet_id or sheet.price_sheet_id,
            analysis_price_sheet_id=analysis_sheet.price_sheet_id,
            source_tokens=source_tokens,
            repriced=repriced,
            reprice_blocked=reprice_blocked,
            credits=sheet.credits_or_refunds,
        )

    tier = record.get("speed_or_service_tier")
    tier_text = tier.strip() if isinstance(tier, str) and tier.strip() else None
    prices, basis, problem = _merge_tier_prices(
        sheet, tier=tier_text, context_tokens=context_tokens
    )
    if problem:
        return _unknown(
            problem,
            price_sheet_id=sheet.price_sheet_id,
            original_price_sheet_id=original_price_sheet_id or sheet.price_sheet_id,
            analysis_price_sheet_id=analysis_sheet.price_sheet_id,
            source_tokens=source_tokens,
            basis=basis,
            repriced=repriced,
            reprice_blocked=reprice_blocked,
            credits=sheet.credits_or_refunds,
        )

    line_items: list[LineItem] = []
    total = Decimal(0)
    missing: list[str] = []
    for tokens_field, price_field, label in _BUCKETS:
        bucket_tokens = _as_int(record.get(tokens_field))
        if bucket_tokens is None:
            continue
        price = prices[price_field]
        if bucket_tokens == 0:
            # A zero bucket needs no price and contributes exactly zero.
            line_items.append(
                LineItem(label, 0, tokens_field, price_field, price, Decimal(0))
            )
            continue
        if price is None:
            missing.append(price_field)
            line_items.append(LineItem(label, bucket_tokens, tokens_field, price_field, None, None))
            continue
        cost = (Decimal(bucket_tokens) * price / _MILLION).quantize(_CENTS_UNIT)
        total += cost
        line_items.append(
            LineItem(label, bucket_tokens, tokens_field, price_field, price, cost)
        )

    if missing:
        return _unknown(
            f"price_missing:{','.join(missing)}",
            price_sheet_id=sheet.price_sheet_id,
            original_price_sheet_id=original_price_sheet_id or sheet.price_sheet_id,
            analysis_price_sheet_id=analysis_sheet.price_sheet_id,
            source_tokens=source_tokens,
            line_items=line_items,
            basis=basis,
            repriced=repriced,
            reprice_blocked=reprice_blocked,
            credits=sheet.credits_or_refunds,
        )

    if not line_items:
        return _unknown(
            "no_token_fields",
            price_sheet_id=sheet.price_sheet_id,
            original_price_sheet_id=original_price_sheet_id or sheet.price_sheet_id,
            analysis_price_sheet_id=analysis_sheet.price_sheet_id,
            source_tokens=source_tokens,
            basis=basis,
            repriced=repriced,
            reprice_blocked=reprice_blocked,
            credits=sheet.credits_or_refunds,
        )

    terms = " + ".join(item.formula() for item in line_items)
    cost = total.quantize(_CENTS_UNIT)
    evidence = EVIDENCE_OFFICIAL if sheet.has_source() else EVIDENCE_UNRESOLVED

    return PricedRequest(
        cost=cost,
        cost_status="priced",
        unknown_reason=None,
        price_sheet_id=sheet.price_sheet_id,
        original_price_sheet_id=original_price_sheet_id or sheet.price_sheet_id,
        analysis_price_sheet_id=analysis_sheet.price_sheet_id,
        repriced=repriced,
        reprice_blocked=reprice_blocked,
        formula=f"C_realized_request = {terms} = {format_usd(cost)}",
        evidence_status=evidence,
        source_tokens=source_tokens,
        line_items=tuple(line_items),
        credits_or_refunds=dict(sheet.credits_or_refunds),
        basis=basis,
    )


# ---------------------------------------------------------------------------
# Persistence. Priced rows live beside the ledger rows they describe, in their
# own table, so the frozen section 6.1 request record stays purely observed.
# ---------------------------------------------------------------------------

PRICED_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS priced_record (
    record_uid INTEGER PRIMARY KEY
        REFERENCES request_record (record_uid) ON DELETE CASCADE,
    priced_utc TEXT NOT NULL,
    cost_usd REAL,
    cost_usd_exact TEXT,
    cost_status TEXT NOT NULL,
    unknown_reason TEXT,
    price_sheet_id TEXT,
    original_price_sheet_id TEXT,
    analysis_price_sheet_id TEXT,
    repriced INTEGER NOT NULL DEFAULT 0,
    reprice_blocked INTEGER NOT NULL DEFAULT 0,
    price_basis TEXT,
    evidence_status TEXT NOT NULL,
    formula TEXT NOT NULL,
    source_tokens_json TEXT NOT NULL,
    line_items_json TEXT NOT NULL,
    credits_or_refunds_json TEXT NOT NULL,
    dollar_label TEXT NOT NULL
);
"""


def ensure_priced_table(conn: sqlite3.Connection) -> None:
    conn.execute(PRICED_RECORD_DDL)
    conn.commit()


def load_priced_record(conn: sqlite3.Connection, record_uid: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM priced_record WHERE record_uid = ?", (record_uid,)
    ).fetchone()
    return dict(row) if row is not None else None


def store_priced_record(
    conn: sqlite3.Connection,
    record_uid: int,
    priced: PricedRequest,
    *,
    priced_utc: str,
) -> None:
    """Persist one priced request. Both price sheet ids are always written."""
    import json

    ensure_priced_table(conn)
    conn.execute(
        """
        INSERT INTO priced_record (
            record_uid, priced_utc, cost_usd, cost_usd_exact, cost_status,
            unknown_reason, price_sheet_id, original_price_sheet_id,
            analysis_price_sheet_id, repriced, reprice_blocked, price_basis,
            evidence_status, formula, source_tokens_json, line_items_json,
            credits_or_refunds_json, dollar_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (record_uid) DO UPDATE SET
            priced_utc = excluded.priced_utc,
            cost_usd = excluded.cost_usd,
            cost_usd_exact = excluded.cost_usd_exact,
            cost_status = excluded.cost_status,
            unknown_reason = excluded.unknown_reason,
            price_sheet_id = excluded.price_sheet_id,
            original_price_sheet_id = excluded.original_price_sheet_id,
            analysis_price_sheet_id = excluded.analysis_price_sheet_id,
            repriced = excluded.repriced,
            reprice_blocked = excluded.reprice_blocked,
            price_basis = excluded.price_basis,
            evidence_status = excluded.evidence_status,
            formula = excluded.formula,
            source_tokens_json = excluded.source_tokens_json,
            line_items_json = excluded.line_items_json,
            credits_or_refunds_json = excluded.credits_or_refunds_json,
            dollar_label = excluded.dollar_label
        """,
        (
            record_uid,
            priced_utc,
            None if priced.is_unknown else float(priced.cost),
            None if priced.is_unknown else str(priced.cost),
            priced.cost_status,
            priced.unknown_reason,
            priced.price_sheet_id,
            priced.original_price_sheet_id,
            priced.analysis_price_sheet_id,
            int(priced.repriced),
            int(priced.reprice_blocked),
            priced.basis,
            priced.evidence_status,
            priced.formula,
            json.dumps(priced.source_tokens, sort_keys=True),
            json.dumps([item.__dict__ | {"price_per_million": (
                None if item.price_per_million is None else str(item.price_per_million)
            ), "cost": None if item.cost is None else str(item.cost)}
                for item in priced.line_items], sort_keys=True),
            json.dumps(priced.credits_or_refunds, sort_keys=True),
            priced.dollar_label,
        ),
    )
    conn.commit()


def price_and_store(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    registry: PriceRegistry,
    *,
    record_uid: int | None = None,
    allow_repricing: bool = False,
    price_sheet_id: str | None = None,
    context_tokens: int | None = None,
    priced_utc: str | None = None,
) -> PricedRequest:
    """Price one ledger row and persist the priced record.

    The row's existing ``original_price_sheet_id`` is read back first, so a
    second pass over the same ledger cannot silently reprice it.
    """
    uid = record_uid if record_uid is not None else _as_int(record.get("record_uid"))
    prior_original: str | None = None
    if uid is not None:
        ensure_priced_table(conn)
        prior = load_priced_record(conn, uid)
        if prior is not None:
            prior_original = prior["original_price_sheet_id"]

    priced = price_request(
        record,
        registry,
        price_sheet_id=price_sheet_id,
        original_price_sheet_id=prior_original,
        allow_repricing=allow_repricing,
        context_tokens=context_tokens,
    )
    if uid is not None:
        store_priced_record(conn, uid, priced, priced_utc=priced_utc or "")
    return priced