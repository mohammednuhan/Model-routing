# Amendments

Reproducibility and provider-semantics amendments to `TAMIAS-FINAL-ARCHITECTURE.md`.
No new subsystem, decision variable, or product surface is added here. Each entry
exists because an implementation forced a choice the frozen text left open.

## A1 - Price sheet identity (spec 9.1/9.2)

Section 9.1 lists the fields a sheet contains and 9.2 requires
`original_price_sheet_id` and `analysis_price_sheet_id` on every priced record,
but does not say where a sheet's id comes from.

Decision: the id is **derived**, not declared:

```text
<provider>/<model>@<accessed>
```

An absent provider, model or accessed date becomes `UNKNOWN` in the id. A
derived id cannot drift from the sheet that names it, and no sheet field is
added beyond the 9.1 list.

## A2 - Repricing requires an explicit opt-in (spec 9.2)

9.2 says never silently replace historical prices. Silent is the operative word,
so the guard is implemented as:

- a row keeps pricing at its stored `original_price_sheet_id`;
- requesting a different sheet sets `reprice_blocked = 1` and the original sheet
  still prices the row;
- re-pricing requires `allow_repricing=True`, and then `repriced = 1` while both
  ids and both amounts are retained.

A second pass of `price_and_store` over the same ledger therefore cannot move a
historical dollar figure.

## A3 - Unsplit cache-write totals are UNKNOWN (spec 9.3)

9.3 requires separate 5m/1h pricing "when 5m and 1h cache-write fields are
exposed". A log that exposes only a combined `cache_write_tokens` does not
identify the bucket, and 9.1 has no total-write price to fall back on.

Decision: a non-zero unsplit write total yields
`C_realized_request = UNKNOWN` with reason `cache_write_basis_unknown` rather
than assuming the 5m basis. This costs coverage on logs without split fields and
buys a figure that is not a guess.

## A4 - Evidence status for dollar outputs

`evidence_status` uses the spec P4 vocabulary, restricted to the two values
section 9 can honestly derive from a sheet:

- `official_documentation` when the sheet has both `accessed` and `source_url`
  and every price the request needed was non-null;
- `unresolved` otherwise, including every UNKNOWN cost.

`inference` is never produced by pricing, because section 9 does no inference.

## A5 - Tiered pricing needs an established basis

- `speed_or_service_tiers` is selected from the request's recorded
  `speed_or_service_tier`. A tier absent from the sheet, or listed with no
  price keys, is `UNKNOWN` (`service_tier_not_in_sheet:*`,
  `service_tier_prices_unknown:*`) instead of falling back to base prices.
- `long_context_tiers` applies only when the caller states the request's
  context length. Tamias does not infer context length from token sums, so
  without a stated length the base prices apply and the basis string records
  `context_length_unknown`.

## A6 - Credits and refunds are surfaced, never applied

`credits_or_refunds` is reported next to the cost and marked not applied. A
credit is not a per-token price; applying it would change a dollar figure
without a token quantity beside it, which the product's receipt contract
forbids.

## A7 - Priced rows live in their own table

Section 6.1 freezes the `request_record` field list. Price columns were not
added to it; `priced_record` is a separate table keyed by `record_uid`, so the
observed ledger row stays purely observed and can be re-priced or audited
against its priced row.