# Price sheets (spec section 9)

One dated YAML file per provider + model. Copy `TEMPLATE.yaml` and fill it in.

## Fields

Exactly the section 9.1 fields, all required in every sheet:

```yaml
provider:
model:
accessed:
source_url:
input_per_million:
output_per_million:
cache_read_per_million:
cache_write_5m_per_million:
cache_write_1h_per_million:
long_context_tiers: []
speed_or_service_tiers: {}
credits_or_refunds: {}
```

`src/tamias/core/pricing.py` refuses to load a sheet that is missing any of
these keys, so a typo cannot silently become a null price.

## No prices are shipped

Every shipped sheet has all five prices `null` with a `TODO(owner)` comment.
Tamias does not guess prices (AGENTS rule 3). Until a sheet is filled, every
request that needs that price returns `C_realized_request = UNKNOWN` -- never
`0.00`. Filling a sheet is the only thing that changes a dollar output.

## Price sheet id

A sheet has no id field of its own. The id is derived deterministically from
its own contents so it cannot drift from the sheet it names:

```text
<provider>/<model>@<accessed>
```

Both `original_price_sheet_id` and `analysis_price_sheet_id` (spec 9.2) are
stored on every priced record. Pricing a row again under a different sheet
never happens silently: the original sheet keeps pricing the row unless the
caller explicitly allows a re-analysis, and both ids stay on the record.

## Matching

A request is priced by its recorded `provider` + `model_id`. If the model id is
absent or `unknown`, the result is `UNKNOWN` with reason
`model_not_identified`. If two sheets match one `(provider, model)` pair with
different `accessed` dates, the result is `UNKNOWN` with reason
`ambiguous_price_sheet` unless the caller names the sheet id.

## Cache writes (spec 9.3)

When a log exposes `cache_write_5m_tokens` and `cache_write_1h_tokens`, the two
buckets are priced separately at their own per-million prices.

When a log exposes only a combined `cache_write_tokens` total, the applicable
bucket is not identifiable from the record, so the cost is `UNKNOWN` with
reason `cache_write_basis_unknown` rather than an assumed 5m basis.

## Every dollar output carries

Source tokens, price sheet id, formula, evidence status, and the label:

> API-equivalent list-price estimate, not a subscription bill

`evidence_status` is `official_documentation` only when the sheet has both
`accessed` and `source_url` and every price the request needed is non-null.
Otherwise it is `unresolved`.