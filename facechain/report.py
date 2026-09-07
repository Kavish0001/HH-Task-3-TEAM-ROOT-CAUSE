"""Console rendering and run artifacts.

Everything the pipeline prints goes through here so the screen recording reads
cleanly: one panel per stage, red reserved for failure and for the tamper demo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from facechain import config

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _console = Console(highlight=False)
    RICH = True
except Exception:  # pragma: no cover - rich is a soft dependency
    _console = None
    RICH = False


ACCENT = "bold red"
OK = "bold green"
DIM = "dim"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(msg: str) -> None:
    print(msg)


def banner() -> None:
    title = "FACECHAIN"
    sub = "Face Identification and Blockchain Verification Pipeline"
    if not RICH:
        _plain("=" * 74)
        _plain("  " + title + " - " + sub)
        _plain("=" * 74)
        return
    text = Text()
    text.append(title + "\n", style="bold white on red")
    text.append(sub, style="dim white")
    _console.print(Panel(text, border_style=ACCENT, padding=(1, 4)))


def stage(number: int, name: str, subtitle: str = "") -> None:
    label = "STAGE %d  %s" % (number, name.upper())
    if not RICH:
        _plain("\n" + "-" * 74)
        _plain("  " + label + ("  |  " + subtitle if subtitle else ""))
        _plain("-" * 74)
        return
    text = Text(label, style="bold white")
    if subtitle:
        text.append("   " + subtitle, style="dim")
    _console.print()
    _console.print(Panel(text, border_style=ACCENT, padding=(0, 2)))


def step(msg: str) -> None:
    if RICH:
        _console.print("  [dim]|[/dim] " + msg)
    else:
        _plain("  | " + msg)


def good(msg: str) -> None:
    if RICH:
        _console.print("  [bold green]OK[/bold green]  " + msg)
    else:
        _plain("  OK  " + msg)


def bad(msg: str) -> None:
    if RICH:
        _console.print("  [bold red]FAIL[/bold red]  " + msg)
    else:
        _plain("  FAIL  " + msg)


def warn(msg: str) -> None:
    if RICH:
        _console.print("  [yellow]![/yellow]  " + msg)
    else:
        _plain("  !  " + msg)


def kv_table(title: str, rows: Dict[str, Any]) -> None:
    if not RICH:
        _plain("\n  " + title)
        for k, v in rows.items():
            _plain("    %-22s %s" % (k, v))
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(style="white", overflow="fold")
    for k, v in rows.items():
        table.add_row(str(k), str(v))
    _console.print(Panel(table, title="[bold]" + title + "[/bold]",
                         border_style="grey37", padding=(1, 1)))


def verdict(passed: bool, headline: str, detail: str = "") -> None:
    if not RICH:
        _plain("\n  [%s] %s" % ("VERIFIED" if passed else "TAMPER DETECTED", headline))
        if detail:
            _plain("        " + detail)
        return
    style = OK if passed else ACCENT
    tag = " VERIFIED " if passed else " TAMPER DETECTED "
    text = Text()
    text.append(tag, style="bold white on green" if passed else "bold white on red")
    text.append("  " + headline, style=style)
    if detail:
        text.append("\n" + detail, style="dim")
    _console.print(Panel(text, border_style=style, padding=(1, 2)))


def rule(msg: str = "") -> None:
    if RICH:
        _console.rule("[dim]" + msg + "[/dim]" if msg else "")
    else:
        _plain("-" * 74 + ("  " + msg if msg else ""))


# --- Run artifacts -------------------------------------------------------

def write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def save_run(payload: Dict[str, Any], name: Optional[str] = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.OUT_DIR / (name or ("run-" + stamp + ".json"))
    return write_json(path, payload)
