"""Tamias command-line interface.

Commands follow spec section 5.3. ``scan`` is implemented; ``report``,
``receipt``, ``doctor`` and ``probe`` are declared stubs.

No command in this module performs network calls.
"""

from __future__ import annotations

import json
import sys

import click

from .core import scan as scan_mod


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def app() -> None:
    """Tamias: transition-aware inference economics for coding agents."""


@app.command()
@click.option(
    "--log-root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Directory searched recursively for *.jsonl session logs.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    help="Path of the SQLite ledger to build.",
)
@click.option(
    "--file",
    "files",
    type=click.Path(dir_okay=False, path_type=str),
    multiple=True,
    help="Parse only this log file (repeatable). Overrides log-root discovery.",
)
@click.option("--run-id", default=None, help="Optional run identifier for ledger rows.")
@click.option("--json", "as_json", is_flag=True, help="Emit diagnostics as JSON.")
def scan(
    log_root: str | None,
    db_path: str | None,
    files: tuple[str, ...],
    run_id: str | None,
    as_json: bool,
) -> None:
    """Build the canonical ledger from local session logs and report diagnostics."""
    diagnostics = scan_mod.scan(
        log_root=log_root,
        db_path=db_path,
        files=list(files) or None,
        run_id=run_id,
    )
    payload = diagnostics.as_dict()

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    click.echo("PARSER DIAGNOSTICS")
    click.echo(f"  parser version      : {payload['parser_version']}")
    click.echo(f"  schema id           : {payload['schema_id']}")
    click.echo(f"  schema status       : {payload['schema_status']}")
    click.echo(f"  log root            : {payload['log_root']}")
    click.echo(f"  ledger              : {payload['db_path']}")
    click.echo("")
    click.echo("INPUT")
    click.echo(f"  files found         : {payload['files_found']}")
    click.echo(f"  files read          : {payload['files_read']}")
    click.echo(f"  lines read          : {payload['lines_read']}")
    click.echo(f"  blank lines         : {payload['blank_lines']}")
    click.echo(f"  records seen        : {payload['records_seen']}")
    click.echo("")
    click.echo("LEDGER")
    click.echo(f"  inserted            : {payload['records_inserted']}")
    click.echo(f"  duplicates skipped  : {payload['duplicates_skipped']}")
    click.echo(f"  dedup conflicts     : {payload['dedup_conflicts']}")
    click.echo(f"  dropped             : {payload['dropped_records']}")
    click.echo(f"  unsupported records : {payload['unsupported_records']}")
    click.echo(f"  zero-usage records  : {payload['zero_usage_records']}")
    click.echo(f"  synthetic records   : {payload['synthetic_records']}")
    click.echo("")
    click.echo("EFFORT (spec Gate B)")
    click.echo(f"  effort recorded     : {payload['effort_recorded']}")
    click.echo(f"  effort unknown      : {payload['effort_unknown']}")
    click.echo("")
    click.echo("DEDUP KEY BASIS")
    for name, value in payload["dedup_key_kind_counts"].items():
        click.echo(f"  {name:<20}: {value}")
    click.echo("")
    click.echo("REQUEST BUCKET")
    for name, value in payload["bucket_counts"].items():
        click.echo(f"  {name:<20}: {value}")
    click.echo("")
    click.echo("RECORD TYPES")
    for name, value in payload["record_type_counts"].items():
        click.echo(f"  {name:<20}: {value}")
    if payload["drop_reason_counts"]:
        click.echo("")
        click.echo("DROP REASONS")
        for name, value in payload["drop_reason_counts"].items():
            click.echo(f"  {name:<28}: {value}")
    if payload["notes"]:
        click.echo("")
        click.echo("NOTES")
        for note in payload["notes"]:
            click.echo(f"  - {note}")


@app.command()
@click.option("--last", type=int, default=None, help="Report the last N sessions.")
@click.option("--session", default=None, help="Report a single session id.")
@click.option("--since", default=None, help="Report sessions since a date.")
def report(last: int | None, session: str | None, since: str | None) -> None:
    """Report session totals and the ranked rebuild ledger (not implemented)."""
    click.echo("report: not implemented yet (spec 5.4). No network calls are made.")


@app.command()
@click.argument("session", required=False)
@click.option("--event", type=int, default=None, help="Receipt for a single event index.")
def receipt(session: str | None, event: int | None) -> None:
    """Print a per-event receipt (not implemented)."""
    click.echo("receipt: not implemented yet (spec 5.5). No network calls are made.")


@app.command()
def doctor() -> None:
    """Check the local environment and ledger health (not implemented)."""
    click.echo("doctor: not implemented yet. No network calls are made.")


@app.command()
def probe() -> None:
    """Opt-in provider probe (not implemented).

    Any command that can spend money defaults to a mock provider. Real runs
    require --real, --max-spend and interactive confirmation, and abort at the
    cap. Those guardrails are not implemented because no probe exists yet.
    """
    click.echo(
        "probe: not implemented. Real runs would require --real, --max-spend and "
        "interactive confirmation, and would abort at the cap."
    )


def main() -> None:
    try:
        app()
    except click.ClickException as exc:  # pragma: no cover - click formats these
        exc.show()
        sys.exit(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
