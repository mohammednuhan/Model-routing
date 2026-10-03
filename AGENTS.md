# AGENTS.md - Tamias Agent Guidelines

## Rules (as specified)

1. **The spec is frozen.** This codebase follows TAMIAS-FINAL-ARCHITECTURE.md. Do not add features or redesign it. Only bug fixes, parser corrections, provider-semantics corrections, and reproducibility amendments are allowed. Any new scope must remove equal scope.

2. **The product never persists prompt text, code, tool arguments or assistant text.** The ledger only stores metadata. Verify this in tests.

3. **Anything not establishable is the string UNKNOWN, never a guess.** Do not fabricate values. Use NULL/UNKNOWN as appropriate.

4. **Observed cost, estimated exposure and causal effect are separate quantities.** Keep these concepts distinct in code and documentation.

5. **Every dollar value shows source tokens, price sheet id, formula, evidence status.** Receipts must be explicit and traceable.

6. **Any command that can spend money defaults to a mock provider; real runs need --real, --max-spend and interactive confirmation, and abort at the cap.** Safety guardrails are mandatory.

## Additional Guidelines

- Use Python 3.11+.
- SQLite from the standard library.
- CLI via click or typer.
- No network calls in scan/report/receipt/doctor.
- Follow the spec precisely - field names and semantics matter.
- Never log or commit secrets.
- Test with pytest.
- When in doubt, record UNKNOWN rather than guessing.
