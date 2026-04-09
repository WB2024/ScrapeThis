"""
Audio Search Module — Philosophize This! Transcript Scraper
------------------------------------------------------------
Provides audio indexing (via faster-whisper), keyword search across
cached word-level transcriptions, and mpv playback at specific timestamps.
"""

import json
import re
import signal
import subprocess
import shutil
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

console = Console()

# ── Whisper model catalogue ───────────────────────────────────────────────────

WHISPER_MODELS = [
    # Standard models
    {"name": "tiny",       "params": "39M",   "size": "~75 MB",  "ram": "~0.5 GB",    "speed": "4–5x real-time",     "accuracy": "Low",        "en_only": "tiny.en"},
    {"name": "base",       "params": "74M",   "size": "~145 MB", "ram": "~0.5–1 GB",  "speed": "2–3x real-time",     "accuracy": "Fair",       "en_only": "base.en"},
    {"name": "small",      "params": "244M",  "size": "~465 MB", "ram": "~1–2 GB",    "speed": "1–2x real-time",     "accuracy": "Good",       "en_only": "small.en"},
    {"name": "medium",     "params": "769M",  "size": "~1.5 GB", "ram": "~2–4 GB",    "speed": "0.5–1x real-time",   "accuracy": "Very Good",  "en_only": "medium.en"},
    {"name": "large-v1",   "params": "1.55B", "size": "~3 GB",   "ram": "~4–6 GB",    "speed": "0.2–0.5x real-time", "accuracy": "Excellent",  "en_only": None},
    {"name": "large-v2",   "params": "1.55B", "size": "~3 GB",   "ram": "~4–6 GB",    "speed": "0.2–0.5x real-time", "accuracy": "Excellent",  "en_only": None},
    {"name": "large-v3",   "params": "1.55B", "size": "~3 GB",   "ram": "~6–8 GB",    "speed": "0.2–0.5x real-time", "accuracy": "Best",       "en_only": None},
    # Distil & Turbo models
    {"name": "distil-small.en",  "params": "122M",  "size": "~250 MB", "ram": "~1 GB",     "speed": "3–4x real-time",     "accuracy": "Good",       "en_only": "EN only"},
    {"name": "distil-medium.en", "params": "384M",  "size": "~750 MB", "ram": "~1.5–2 GB", "speed": "1.5–2.5x real-time", "accuracy": "Very Good",  "en_only": "EN only"},
    {"name": "distil-large-v2",  "params": "756M",  "size": "~1.5 GB", "ram": "~2–3 GB",   "speed": "1.5–2x real-time",   "accuracy": "Excellent",  "en_only": "Multilingual"},
    {"name": "distil-large-v3",  "params": "756M",  "size": "~1.5 GB", "ram": "~2–3 GB",   "speed": "2–3x real-time",     "accuracy": "Excellent",  "en_only": "Multilingual"},
    {"name": "large-v3-turbo",   "params": "809M",  "size": "~1.6 GB", "ram": "~2–3 GB",   "speed": "3–5x real-time",     "accuracy": "Near-best",  "en_only": "Multilingual"},
]

WHISPER_MODEL_NAMES = [m["name"] for m in WHISPER_MODELS]
DEFAULT_WHISPER_MODEL = "base"


def show_model_table() -> None:
    """Print the full model catalogue as a Rich table."""
    table = Table(title="Available Whisper Models", header_style="bold magenta", padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Model", style="cyan bold")
    table.add_column("Params", justify="right")
    table.add_column("Download", justify="right")
    table.add_column("RAM (CPU)", justify="right")
    table.add_column("CPU Speed", justify="right")
    table.add_column("Accuracy", style="green")
    table.add_column("EN-only variant")

    for i, m in enumerate(WHISPER_MODELS, 1):
        table.add_row(
            str(i),
            m["name"],
            m["params"],
            m["size"],
            m["ram"],
            m["speed"],
            m["accuracy"],
            m["en_only"] or "✗",
        )
    console.print(table)


def choose_whisper_model(current: str) -> str:
    """Interactive model selector. Returns the chosen model name."""
    console.print(f"\n[dim]Current model:[/dim] [bold cyan]{current}[/bold cyan]\n")
    show_model_table()

    raw = Prompt.ask(
        "\n[bold]Select model number or name[/bold] [dim](or press Enter to keep current)[/dim]",
        default="",
    ).strip()

    if not raw:
        return current

    # Accept by number
    if raw.isdigit() and 1 <= int(raw) <= len(WHISPER_MODELS):
        chosen = WHISPER_MODELS[int(raw) - 1]["name"]
        console.print(f"[green]✓ Whisper model set to:[/green] [bold]{chosen}[/bold]")
        return chosen

    # Accept by name (exact or close)
    if raw in WHISPER_MODEL_NAMES:
        console.print(f"[green]✓ Whisper model set to:[/green] [bold]{raw}[/bold]")
        return raw

    console.print(f"[red]Unknown model: '{raw}'. Keeping {current}.[/red]")
    return current


# ── Transcription & Indexing ──────────────────────────────────────────────────

def _get_index_dir(audio_path: Path) -> Path:
    """Return the hidden index directory inside audio_path."""
    return audio_path / ".audio_index"


# Module-level flag for graceful interrupt
_interrupted = False


def _handle_interrupt(signum, frame):
    """Signal handler that sets the interrupt flag instead of raising."""
    global _interrupted
    _interrupted = True


def transcribe(audio_file: Path, model_name: str = DEFAULT_WHISPER_MODEL) -> list[dict]:
    """Transcribe a single audio file and return a list of {word, start} dicts."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_file), word_timestamps=True)
    words = []
    for segment in segments:
        if _interrupted:
            break
        if segment.words is None:
            continue
        for w in segment.words:
            cleaned = w.word.strip().lower().strip(".,!?;:\"'()-[]{}…")
            if cleaned:
                words.append({"word": cleaned, "start": round(w.start, 2)})
    return words


def index_audio_files(audio_path: Path, force: bool = False, model_name: str = DEFAULT_WHISPER_MODEL) -> int:
    """
    Index all .mp3 files in audio_path. Skips already-indexed files unless force=True.
    Handles Ctrl+C gracefully — saves progress and resumes on next run.
    Returns the number of newly indexed files.
    """
    global _interrupted
    _interrupted = False

    index_dir = _get_index_dir(audio_path)
    index_dir.mkdir(parents=True, exist_ok=True)

    mp3_files = sorted(audio_path.glob("*.mp3"))
    if not mp3_files:
        console.print("[yellow]No audio files found. Run option 6 to download.[/yellow]")
        return 0

    to_index = []
    for mp3 in mp3_files:
        cache_file = index_dir / (mp3.stem + ".json")
        if force or not cache_file.exists():
            to_index.append(mp3)

    if not to_index:
        console.print(f"[bold green]✓ All {len(mp3_files)} audio file(s) already indexed.[/bold green]")
        return 0

    console.print(
        f"\n[green]{len(mp3_files)} audio file(s) found.[/green] "
        f"[yellow]{len(to_index)} to index.[/yellow]"
    )
    console.print(f"[dim]Model: {model_name} — Press Ctrl+C at any time to stop safely.[/dim]\n")

    indexed = 0
    failed: list[str] = []

    # Install our graceful interrupt handler
    prev_handler = signal.signal(signal.SIGINT, _handle_interrupt)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Indexing audio files...", total=len(to_index))

            for mp3 in to_index:
                if _interrupted:
                    console.print(
                        f"\n[bold yellow]⚠ Interrupted![/bold yellow] "
                        f"Indexed {indexed}/{len(to_index)} file(s). "
                        f"Run again to resume where you left off."
                    )
                    break

                progress.update(task, description=f"[cyan]indexing: {mp3.name}")
                try:
                    words = transcribe(mp3, model_name=model_name)
                    if _interrupted and not words:
                        # Interrupted before any words were captured — don't write empty cache
                        console.print(
                            f"\n[bold yellow]⚠ Interrupted![/bold yellow] "
                            f"Indexed {indexed}/{len(to_index)} file(s). "
                            f"Run again to resume where you left off."
                        )
                        break
                    cache_file = index_dir / (mp3.stem + ".json")
                    with open(cache_file, "w") as f:
                        json.dump(words, f)
                    indexed += 1
                    if _interrupted:
                        # Finished the current file but user wants to stop
                        console.print(
                            f"\n[bold yellow]⚠ Interrupted after completing {mp3.name}.[/bold yellow] "
                            f"Indexed {indexed}/{len(to_index)} file(s). "
                            f"Run again to resume where you left off."
                        )
                        break
                except Exception as e:
                    console.print(f"[red]  Failed to index {mp3.name}: {e}[/red]")
                    failed.append(mp3.name)
                progress.advance(task)
    finally:
        # Restore the original signal handler
        signal.signal(signal.SIGINT, prev_handler)

    if not _interrupted:
        console.print(f"\n[bold green]✓ Indexed {indexed} audio file(s)[/bold green] → {index_dir}")
    if failed:
        console.print(f"[yellow]Failed ({len(failed)}): {', '.join(failed)}[/yellow]")

    return indexed


# ── Search ────────────────────────────────────────────────────────────────────

def search_audio(keyword: str, audio_path: Path) -> list[dict]:
    """
    Search the cached index for a keyword.
    Returns list of {filename, path, timestamps, count} sorted by count desc.
    """
    index_dir = _get_index_dir(audio_path)
    keyword_lower = keyword.lower().strip()
    results = []

    if not index_dir.exists():
        return results

    for cache_file in sorted(index_dir.glob("*.json")):
        try:
            with open(cache_file) as f:
                words = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        hits = [w["start"] for w in words if w["word"] == keyword_lower]
        if hits:
            audio_name = cache_file.stem
            audio_file = audio_path / (audio_name + ".mp3")
            # Skip orphaned cache files (mp3 deleted)
            if not audio_file.exists():
                continue
            results.append({
                "filename": audio_name + ".mp3",
                "path": audio_file,
                "timestamps": hits,
                "count": len(hits),
            })

    results.sort(key=lambda x: x["count"], reverse=True)
    return results


# ── Playback ──────────────────────────────────────────────────────────────────

def open_in_mpv(audio_path: Path, timestamp: float) -> bool:
    """Launch mpv at the given timestamp. Returns True on success."""
    if not shutil.which("mpv"):
        console.print("[red]mpv not found. Install with: sudo apt install mpv[/red]")
        return False
    subprocess.Popen(
        ["mpv", f"--start={timestamp:.2f}", str(audio_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


# ── Timestamp formatting ─────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS string."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── TUI flows (called from scrape_philosophize.py) ────────────────────────────

def run_index_audio(config: dict) -> None:
    """Menu Option 8 — Index audio files."""
    audio_path_str = config.get("audio_path")
    if not audio_path_str:
        console.print("[yellow]Audio path not set. Run option 6 first.[/yellow]")
        return

    audio_path = Path(audio_path_str)
    if not audio_path.exists():
        console.print(f"[yellow]Audio path does not exist: {audio_path}[/yellow]")
        return

    model_name = config.get("whisper_model", DEFAULT_WHISPER_MODEL)
    index_audio_files(audio_path, force=False, model_name=model_name)


def run_search_audio(config: dict) -> None:
    """Menu Option 8 — Search audio by keyword."""
    audio_path_str = config.get("audio_path")
    if not audio_path_str:
        console.print("[yellow]Audio path not set. Run option 6 first.[/yellow]")
        return

    audio_path = Path(audio_path_str)
    if not audio_path.exists():
        console.print(f"[yellow]Audio path does not exist: {audio_path}[/yellow]")
        return

    keyword = Prompt.ask("\n[bold]Enter keyword to search[/bold]").strip()
    if not keyword:
        console.print("[yellow]No keyword entered.[/yellow]")
        return

    # Auto-index any new (un-indexed) files before searching
    index_dir = _get_index_dir(audio_path)
    mp3_files = list(audio_path.glob("*.mp3"))
    unindexed = [
        mp3 for mp3 in mp3_files
        if not (index_dir / (mp3.stem + ".json")).exists()
    ]
    if unindexed:
        model_name = config.get("whisper_model", DEFAULT_WHISPER_MODEL)
        console.print(f"\n[dim]Auto-indexing {len(unindexed)} new audio file(s)…[/dim]")
        index_audio_files(audio_path, force=False, model_name=model_name)

    results = search_audio(keyword, audio_path)

    if not results:
        console.print(f"[yellow]'{keyword}' not found in any audio file.[/yellow]")
        return

    while True:
        console.print(
            f"\n[bold green]'{keyword}' found in {len(results)} audio file(s):[/bold green]\n"
        )
        table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
        table.add_column("#", style="dim", width=4)
        table.add_column("Episode", style="cyan")
        table.add_column("Occurrences", justify="right", style="bold yellow")
        for i, r in enumerate(results, 1):
            table.add_row(str(i), r["filename"].replace(".mp3", ""), str(r["count"]))
        console.print(table)

        raw = Prompt.ask(
            "\n[bold]Select episode number[/bold] [dim](or press Enter to go back)[/dim]",
            default="",
        ).strip()

        if not raw:
            return

        if not raw.isdigit() or not (1 <= int(raw) <= len(results)):
            console.print(f"[red]Please enter a number between 1 and {len(results)}.[/red]")
            continue

        chosen = results[int(raw) - 1]

        # Timestamp selection loop — stays on this episode until user presses Enter
        while True:
            timestamps = chosen["timestamps"]
            console.print(
                f"\n[bold]'{keyword}' in {chosen['filename']}[/bold] — "
                f"{chosen['count']} occurrence(s):\n"
            )

            # Display timestamps in a compact grid (4 per row)
            cols_per_row = 4
            lines = []
            for i, ts in enumerate(timestamps, 1):
                lines.append(f"  [{i:>3}]  {format_timestamp(ts)}")

            rows = []
            for start in range(0, len(lines), cols_per_row):
                rows.append("    ".join(lines[start:start + cols_per_row]))
            console.print("\n".join(rows))

            ts_raw = Prompt.ask(
                "\n[bold]Jump to which occurrence?[/bold] [dim](or press Enter to go back)[/dim]",
                default="",
            ).strip()

            if not ts_raw:
                break  # back to results table

            if not ts_raw.isdigit() or not (1 <= int(ts_raw) <= len(timestamps)):
                console.print(f"[red]Please enter a number between 1 and {len(timestamps)}.[/red]")
                continue

            ts_index = int(ts_raw) - 1
            ts_seconds = timestamps[ts_index]
            console.print(
                f"\n[green]Opening {chosen['filename']} at "
                f"{format_timestamp(ts_seconds)} in mpv...[/green]"
            )
            open_in_mpv(chosen["path"], ts_seconds)


def get_index_count(audio_path_str: str | None) -> int:
    """Return the number of indexed audio files, or 0 if path is unset."""
    if not audio_path_str:
        return 0
    index_dir = _get_index_dir(Path(audio_path_str))
    if not index_dir.exists():
        return 0
    return len(list(index_dir.glob("*.json")))
