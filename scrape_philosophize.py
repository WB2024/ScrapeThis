#!/usr/bin/env python3
"""
Philosophize This! Transcript Scraper
--------------------------------------
A TUI-driven scraper for https://www.philosophizethis.org/transcript
- Prompts for save path on first run, persists config to JSON
- Tracks previously scraped files, only downloads new ones
- Converts HTML -> Markdown, saves one .md file per transcript
"""

import json
import os
import re
import time
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from rich import print as rprint

# ── Constants ────────────────────────────────────────────────────────────────
BASE_URL = "https://www.philosophizethis.org"
TRANSCRIPT_INDEX = f"{BASE_URL}/transcript"
PODCAST_INDEX    = f"{BASE_URL}/podcast"
CONFIG_FILE = Path.home() / ".philosophize_scraper.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
REQUEST_DELAY = 1.5  # seconds between requests, be polite

console = Console()


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"save_path": None, "scraped": []}


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Scraping helpers ──────────────────────────────────────────────────────────

def get_soup(url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            console.print(f"[yellow]  Attempt {attempt}/{retries} failed for {url}: {e}[/yellow]")
            if attempt < retries:
                time.sleep(2 * attempt)
    console.print(f"[red]  Could not fetch {url} after {retries} attempts.[/red]")
    return None


def get_all_transcript_links() -> list[dict]:
    """
    Crawl the paginated index and return a list of
    {"title": ..., "url": ..., "slug": ...} dicts.
    """
    links = []
    page_url = TRANSCRIPT_INDEX
    visited_pages = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Crawling index pages...", total=None)

        while page_url and page_url not in visited_pages:
            visited_pages.add(page_url)
            progress.update(task, description=f"[cyan]Crawling: {page_url}")
            soup = get_soup(page_url)
            if not soup:
                break

            # Collect transcript links
            for a in soup.select("a.blog-more-link"):
                href = a.get("href", "")
                if href.startswith("/transcript/"):
                    full_url = BASE_URL + href
                    slug = href.rstrip("/").split("/")[-1]
                    # Try to grab the title from the nearby heading
                    title = slug  # fallback
                    article = a.find_parent("article") or a.find_parent("div", class_="blog-item")
                    if article:
                        h_tag = article.find(re.compile(r"^h[1-6]$"))
                        if h_tag:
                            title = h_tag.get_text(strip=True)
                    if not any(l["slug"] == slug for l in links):
                        links.append({"title": title, "url": full_url, "slug": slug})

            # Pagination: look for "Older Posts" link
            older_div = soup.find("div", class_="older")
            next_url = None
            if older_div:
                next_a = older_div.find("a")
                if next_a and next_a.get("href"):
                    next_url = BASE_URL + next_a["href"]

            page_url = next_url
            if page_url:
                time.sleep(REQUEST_DELAY)

    return links


def scrape_transcript(url: str) -> str | None:
    """Fetch an individual transcript page and return its Markdown content."""
    soup = get_soup(url)
    if not soup:
        return None

    content_div = soup.find("div", class_="sqs-html-content")
    if not content_div:
        # Fallback: try any main content block
        content_div = soup.find("div", class_=re.compile(r"entry-content|post-content|body-text"))

    if not content_div:
        console.print(f"[yellow]  Warning: no content div found at {url}[/yellow]")
        return None

    raw_html = str(content_div)
    markdown = md(
        raw_html,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )
    # Clean up excessive blank lines
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return markdown


def slugify_filename(slug: str) -> str:
    """Convert a slug to a safe filename."""
    return re.sub(r"[^\w\-]", "_", slug) + ".md"


def slugify_title(title: str) -> str:
    """Convert a podcast episode title into a safe ASCII filename."""
    slug = title.lower()
    slug = slug.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^\w\s-]", " ", slug)
    slug = re.sub(r"[\s_]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug + ".md"


# ── Podcast map helpers ───────────────────────────────────────────────────────

def get_all_podcast_page_urls() -> list[str]:
    """Crawl the paginated podcast index and return all episode detail page URLs."""
    urls: list[str] = []
    page_url: str | None = PODCAST_INDEX
    visited_pages: set[str] = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Crawling podcast index pages...", total=None)

        while page_url and page_url not in visited_pages:
            visited_pages.add(page_url)
            progress.update(task, description=f"[cyan]Crawling: {page_url}")
            soup = get_soup(page_url)
            if not soup:
                break

            for a in soup.select("a.blog-more-link"):
                href = a.get("href", "")
                if href.startswith("/podcast/"):
                    full_url = BASE_URL + href
                    if full_url not in urls:
                        urls.append(full_url)

            older_div = soup.find("div", class_="older")
            next_url = None
            if older_div:
                next_a = older_div.find("a")
                if next_a and next_a.get("href"):
                    next_url = BASE_URL + next_a["href"]

            page_url = next_url
            if page_url:
                time.sleep(REQUEST_DELAY)

    return urls


def build_podcast_map(podcast_urls: list[str]) -> dict[str, dict]:
    """
    Fetch each podcast audio page and return a reverse map of
    {transcript_slug: {"podcast_url": ..., "episode_title": ...}}.
    """
    mapping: dict[str, dict] = {}
    eta_mins = round(len(podcast_urls) * REQUEST_DELAY / 60, 1)
    console.print(f"[dim]Fetching {len(podcast_urls)} podcast pages (~{eta_mins} min)…[/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Building map...", total=len(podcast_urls))

        for url in podcast_urls:
            soup = get_soup(url)
            if soup:
                transcript_a = soup.find("a", href=re.compile(r"/transcript/"))
                if transcript_a:
                    href = transcript_a.get("href", "")
                    slug = href.rstrip("/").split("/")[-1]
                    h1 = soup.find("h1", class_=re.compile(r"entry-title"))
                    episode_title = h1.get_text(strip=True) if h1 else slug
                    mapping[slug] = {"podcast_url": url, "episode_title": episode_title}
            progress.advance(task)
            time.sleep(REQUEST_DELAY)

    return mapping


def enrich_file_with_podcast_url(filepath: Path, podcast_url: str, episode_title: str) -> Path | None:
    """
    Update a transcript .md file with the podcast audio URL and episode title sourced
    from the podcast audio page:
      - Updates title: in frontmatter
      - Inserts podcast_url: in frontmatter
      - Replaces the # heading and inserts a listen link in the body
      - Renames the file to match the episode title
    Returns the (possibly renamed) Path on success, None on failure.
    """
    try:
        content = filepath.read_text(encoding="utf-8")
    except IOError:
        return None

    safe_title = episode_title.replace('"', '\\"')

    # Update title: field in frontmatter
    content = re.sub(
        r'^title:\s*"[^"]*"$',
        f'title: "{safe_title}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )

    # Insert podcast_url before scraped_at in frontmatter
    content = content.replace(
        "\nscraped_at:",
        f'\npodcast_url: "{podcast_url}"\nscraped_at:',
        1,
    )

    # Replace the first # heading line and insert the listen link beneath it
    content = re.sub(
        r"^# .+\n\n",
        f"# {episode_title}\n\n[🎧 **Listen to episode**]({podcast_url})\n\n",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    # Determine new filename derived from the podcast page's episode title
    new_filename = slugify_title(episode_title)
    new_filepath = filepath.parent / new_filename

    # Resolve collision (very unlikely but possible)
    if new_filepath.exists() and new_filepath.resolve() != filepath.resolve():
        stem = new_filename[:-3]
        counter = 1
        while new_filepath.exists():
            new_filepath = filepath.parent / f"{stem}-{counter}.md"
            counter += 1

    try:
        new_filepath.write_text(content, encoding="utf-8")
        if new_filepath.resolve() != filepath.resolve():
            filepath.unlink()
        return new_filepath
    except IOError:
        return None


def run_enrichment(config: dict) -> dict:
    """Crawl all podcast audio pages, build a reverse map, and enrich transcript .md files."""
    save_path = Path(config.get("save_path", ""))
    if not save_path or not save_path.exists():
        console.print("[red]Save path not configured or does not exist.[/red]")
        return config

    console.print("\n[bold cyan]Step 1/3:[/bold cyan] Fetching podcast index…")
    podcast_urls = get_all_podcast_page_urls()

    if not podcast_urls:
        console.print("[red]No podcast pages found. Check your connection.[/red]")
        return config

    console.print(f"[green]Found {len(podcast_urls)} podcast pages.[/green]")
    console.print(f"\n[bold cyan]Step 2/3:[/bold cyan] Building transcript→podcast map…")
    podcast_map = build_podcast_map(podcast_urls)
    console.print(f"[green]Mapped {len(podcast_map)} transcripts.[/green]")

    md_files = list(save_path.glob("*.md"))
    if not md_files:
        console.print("[yellow]No scraped transcripts found in save path.[/yellow]")
        return config

    console.print(f"\n[bold cyan]Step 3/3:[/bold cyan] Enriching {len(md_files)} file(s)…\n")

    enriched = 0
    skipped = 0
    unmatched: list[str] = []
    failed: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Enriching...", total=len(md_files))

        for md_file in md_files:
            try:
                text = md_file.read_text(encoding="utf-8")
            except IOError:
                failed.append(md_file.name)
                progress.advance(task)
                continue

            # Skip already-enriched files (idempotent)
            if "podcast_url:" in text:
                skipped += 1
                progress.advance(task)
                continue

            # Extract transcript slug from stored frontmatter
            slug_match = re.search(r'^slug:\s*"([^"]+)"', text, re.MULTILINE)
            if not slug_match:
                unmatched.append(md_file.name)
                progress.advance(task)
                continue

            slug = slug_match.group(1)
            info = podcast_map.get(slug)

            if not info:
                # No podcast page links to this transcript — record empty URL and warn
                updated = text.replace(
                    "\nscraped_at:",
                    '\npodcast_url: ""\nscraped_at:',
                    1,
                )
                try:
                    md_file.write_text(updated, encoding="utf-8")
                except IOError:
                    pass
                unmatched.append(slug)
                progress.advance(task)
                continue

            progress.update(task, description=f"[cyan]{slug}")
            new_path = enrich_file_with_podcast_url(md_file, info["podcast_url"], info["episode_title"])
            if new_path:
                enriched += 1
            else:
                failed.append(md_file.name)
            progress.advance(task)

    console.print(
        f"\n[bold green]✓ Enriched {enriched} file(s)[/bold green]  "
        f"[dim]{skipped} already done.[/dim]"
    )
    if unmatched:
        console.print(f"\n[yellow]No podcast page found for {len(unmatched)} transcript(s):[/yellow]")
        warn_table = Table(show_header=False, box=None, padding=(0, 2))
        for slug in unmatched:
            warn_table.add_row("[dim]·[/dim]", slug)
        console.print(warn_table)
    if failed:
        console.print(f"[red]Failed to write: {', '.join(failed)}[/red]")

    return config


# ── TUI screens ───────────────────────────────────────────────────────────────

def print_header():
    console.print(
        Panel.fit(
            "[bold magenta]Philosophize This! Transcript Scraper[/bold magenta]\n"
            "[dim]https://www.philosophizethis.org/transcript[/dim]",
            border_style="magenta",
        )
    )


def prompt_for_path(current: str | None = None) -> str:
    if current:
        console.print(f"\n[dim]Current save path:[/dim] [green]{current}[/green]")

    while True:
        raw = Prompt.ask(
            "\n[bold]Enter the directory path to save transcripts[/bold]",
            default=current or str(Path.home() / "philosophize_transcripts"),
        )
        path = Path(raw).expanduser().resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]✓ Save path set to:[/green] {path}")
            return str(path)
        except Exception as e:
            console.print(f"[red]Could not create directory: {e}[/red]")


def show_settings_menu(config: dict) -> dict:
    console.print("\n[bold underline]Settings[/bold underline]")
    console.print(f"  Save path : [green]{config.get('save_path', 'Not set')}[/green]")
    console.print(f"  Scraped   : [cyan]{len(config.get('scraped', []))} transcripts[/cyan]")

    choice = Prompt.ask(
        "\nWhat would you like to do?",
        choices=["change_path", "clear_history", "back"],
        default="back",
    )

    if choice == "change_path":
        config["save_path"] = prompt_for_path(config.get("save_path"))
        save_config(config)
    elif choice == "clear_history":
        if Confirm.ask("[yellow]Clear scrape history? (will re-scrape everything next run)[/yellow]"):
            config["scraped"] = []
            save_config(config)
            console.print("[green]History cleared.[/green]")

    return config


def main_menu(config: dict) -> str:
    console.print("\n[bold underline]Main Menu[/bold underline]")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[cyan]1[/cyan]", "Scrape new transcripts")
    table.add_row("[cyan]2[/cyan]", "Settings / change save path")
    table.add_row("[cyan]3[/cyan]", "Show scraped files")
    table.add_row("[cyan]4[/cyan]", "Search transcripts by keyword")
    table.add_row("[cyan]5[/cyan]", "Enrich transcripts with podcast links")
    table.add_row("[cyan]6[/cyan]", "Exit")
    console.print(table)
    return Prompt.ask("Choose", choices=["1", "2", "3", "4", "5", "6"], default="1")


def show_scraped_files(config: dict):
    scraped = config.get("scraped", [])
    if not scraped:
        console.print("[yellow]No transcripts scraped yet.[/yellow]")
        return
    console.print(f"\n[bold]Scraped transcripts ({len(scraped)}):[/bold]")
    for i, slug in enumerate(scraped, 1):
        console.print(f"  [dim]{i:>3}.[/dim] {slug}")


def search_transcripts(config: dict):
    """Keyword search across all scraped .md files, ranked by mention count."""
    save_path = Path(config.get("save_path", ""))
    if not save_path or not save_path.exists():
        console.print("[red]Save path not configured or does not exist.[/red]")
        return

    keyword = Prompt.ask("\n[bold]Enter keyword to search[/bold]").strip()
    if not keyword:
        console.print("[yellow]No keyword entered.[/yellow]")
        return

    md_files = list(save_path.glob("*.md"))
    if not md_files:
        console.print("[yellow]No scraped transcripts found in save path.[/yellow]")
        return

    console.print(
        f"\n[cyan]Searching [bold]{len(md_files)}[/bold] file(s) "
        f"for '[bold]{keyword}[/bold]'…[/cyan]\n"
    )

    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Scanning files...", total=len(md_files))
        for md_file in md_files:
            try:
                text = md_file.read_text(encoding="utf-8")
                count = len(pattern.findall(text))
                if count > 0:
                    results.append({"file": md_file.stem, "count": count})
            except IOError:
                pass
            progress.advance(task)

    if not results:
        console.print(f"[yellow]No transcripts found containing '{keyword}'.[/yellow]")
        return

    results.sort(key=lambda x: x["count"], reverse=True)

    console.print(
        f"\n[bold green]'{keyword}' found in {len(results)} transcript(s):[/bold green]\n"
    )
    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 2))
    table.add_column("#", style="dim", width=4)
    table.add_column("Episode", style="cyan")
    table.add_column("Mentions", justify="right", style="bold yellow")
    for i, r in enumerate(results, 1):
        table.add_row(str(i), r["file"], str(r["count"]))
    console.print(table)


def run_scrape(config: dict) -> dict:
    save_path = Path(config["save_path"])
    already_scraped: set = set(config.get("scraped", []))

    console.print("\n[bold cyan]Step 1/2:[/bold cyan] Fetching transcript index…")
    all_links = get_all_transcript_links()

    if not all_links:
        console.print("[red]No transcript links found. Check your connection.[/red]")
        return config

    new_links = [l for l in all_links if l["slug"] not in already_scraped]
    console.print(
        f"\n[green]Found {len(all_links)} total transcripts.[/green] "
        f"[yellow]{len(new_links)} new to scrape.[/yellow]"
    )

    if not new_links:
        console.print("[bold green]✓ Everything is up to date![/bold green]")
        return config

    console.print(f"\n[bold cyan]Step 2/2:[/bold cyan] Scraping {len(new_links)} transcripts…\n")

    failed = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Scraping...", total=len(new_links))

        for link in new_links:
            slug = link["slug"]
            title = link["title"]
            url = link["url"]

            progress.update(task, description=f"[cyan]{slug}")

            markdown_content = scrape_transcript(url)

            if markdown_content:
                # Build the file
                filename = slugify_filename(slug)
                filepath = save_path / filename

                # Prepend a YAML-ish frontmatter block
                full_content = (
                    f"---\n"
                    f"title: \"{title}\"\n"
                    f"slug: \"{slug}\"\n"
                    f"source: \"{url}\"\n"
                    f"scraped_at: \"{time.strftime('%Y-%m-%d')}\"\n"
                    f"---\n\n"
                    f"# {title}\n\n"
                    f"{markdown_content}\n"
                )

                try:
                    filepath.write_text(full_content, encoding="utf-8")
                    config["scraped"].append(slug)
                    save_config(config)  # save after each success so progress isn't lost on crash
                except IOError as e:
                    console.print(f"[red]  Failed to write {filename}: {e}[/red]")
                    failed.append(slug)
            else:
                failed.append(slug)

            progress.advance(task)
            time.sleep(REQUEST_DELAY)

    newly_done = len(new_links) - len(failed)
    console.print(f"\n[bold green]✓ Scraped {newly_done} new transcripts[/bold green] → {save_path}")
    if failed:
        console.print(f"[yellow]Failed ({len(failed)}): {', '.join(failed)}[/yellow]")

    return config


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    config = load_config()

    print_header()

    # First-run path setup
    if not config.get("save_path"):
        console.print("\n[bold yellow]First run! Let's set up your save directory.[/bold yellow]")
        config["save_path"] = prompt_for_path()
        save_config(config)

    while True:
        choice = main_menu(config)

        if choice == "1":
            config = run_scrape(config)
        elif choice == "2":
            config = show_settings_menu(config)
        elif choice == "3":
            show_scraped_files(config)
        elif choice == "4":
            search_transcripts(config)
        elif choice == "5":
            config = run_enrichment(config)
        elif choice == "6":
            console.print("[dim]Goodbye![/dim]")
            sys.exit(0)


if __name__ == "__main__":
    main()
