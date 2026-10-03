import json
import sys
from pathlib import Path

try:
    import typer
    app = typer.Typer(help="Tamias: transition-aware inference economics for coding agents.")
except Exception:  # pragma: no cover
    import click

    @click.group()
    def app() -> None:
        """Tamias: transition-aware inference economics for coding agents."""
        pass


@app.command("scan")
def scan_cmd(
    log_root: str = typer.Option(None, help="Path to logs root (defaults to ~/.claude/projects)"),
    db_path: str = typer.Option(None, help="Path to ledger DB"),
    run_id: str = typer.Option(None, help="Optional run id to tag records"),
) -> None:
    """Build the canonical ledger from local session logs and report diagnostics."""
    from .core import scan

    diags = scan.scan(log_root=log_root, db_path=db_path, run_id=run_id)
    print(json.dumps(diags.as_dict(), indent=2))


@app.command("report")
def report_cmd() -> None:
    """Report session totals and the ranked rebuild ledger (not implemented)."""
    typer.echo("tamias report: not implemented in this configuration.", err=True)
    raise typer.Exit(code=1)


@app.command("receipt")
def receipt_cmd() -> None:
    """Print a per-event receipt (not implemented)."""
    typer.echo("tamias receipt: not implemented in this configuration.", err=True)
    raise typer.Exit(code=1)


@app.command("doctor")
def doctor_cmd() -> None:
    """Check the local environment and ledger health (not implemented)."""
    typer.echo("tamias doctor: not implemented in this configuration.", err=True)
    raise typer.Exit(code=1)


@app.command("probe")
def probe_cmd() -> None:
    """Opt-in provider probe (not implemented)."""
    typer.echo("tamias probe: not implemented in this configuration.", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
