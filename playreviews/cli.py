"""Command line for the Play Store review downloader."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

from playreviews import __version__
from playreviews.client import AppNotFound, PlayClient, StoreError
from playreviews.constants import (
    DEFAULT_DELAY_SECONDS,
    DEFAULT_PAGE_SIZE,
    DEVICES,
    MAX_PAGE_SIZE,
    SORTS,
)
from playreviews.download import Config, DownloadError, output_directory, run

APP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
LANG = re.compile(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")


def _log(message: str) -> None:
    print(message, flush=True)


def parse_target(value: str) -> tuple[str, str | None, str | None]:
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        query = urllib.parse.parse_qs(parsed.query)
        app_id = (query.get("id") or [None])[0]
        if not app_id:
            raise SystemExit("That URL has no id parameter. Use a Play Store app details link.")
        lang = (query.get("hl") or [None])[0]
        country = (query.get("gl") or [None])[0]
        return app_id, lang, country
    return value, None, None


def _check_identity(app_id: str, lang: str, country: str) -> None:
    if not APP_ID.fullmatch(app_id):
        raise SystemExit(f"{app_id} is not a Play Store package name, such as com.example.app.")
    if not LANG.fullmatch(lang):
        raise SystemExit(f"{lang} is not a language code such as en or pt-BR.")
    if not COUNTRY.fullmatch(country):
        raise SystemExit(f"{country} is not a two-letter country code such as us.")


def _add_query_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lang", help="Listing language, such as en. Defaults to the URL or en.")
    parser.add_argument("--country", help="Store country, such as us. Defaults to the URL or us.")
    parser.add_argument("--sort", choices=sorted(SORTS), default="newest", help="Review order. Default: newest.")
    parser.add_argument("--score", type=int, choices=[1, 2, 3, 4, 5], help="Only reviews with this star rating.")
    parser.add_argument(
        "--device",
        choices=sorted(DEVICES),
        help="Only reviews left on this device class. Omit it to collect every device.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("downloads"),
        help="Directory for app.json, permissions.json, reviews.jsonl, and checkpoint.json.",
    )


def _build_config(args: argparse.Namespace, downloading: bool) -> Config:
    app_id, url_lang, url_country = parse_target(args.app)
    lang = args.lang or url_lang or "en"
    country = args.country or url_country or "us"
    _check_identity(app_id, lang, country)
    page_size = DEFAULT_PAGE_SIZE
    delay = DEFAULT_DELAY_SECONDS
    burst_every = 10
    max_reviews = None
    fresh = False
    if downloading:
        page_size = args.page_size
        if page_size < 1:
            raise SystemExit("--page-size must be at least 1.")
        if page_size > MAX_PAGE_SIZE:
            _log(f"page size capped at {MAX_PAGE_SIZE} so each request stays near what the site loads")
            page_size = MAX_PAGE_SIZE
        delay = args.delay
        if delay < 0:
            raise SystemExit("--delay cannot be negative.")
        burst_every = args.burst_every
        if burst_every < 0:
            raise SystemExit("--burst-every cannot be negative.")
        max_reviews = args.max
        if max_reviews is not None and max_reviews < 1:
            raise SystemExit("--max must be at least 1.")
        fresh = args.fresh
        if delay == 0:
            _log("delay is 0; the store is more likely to throttle a walk with no pauses")
    return Config(
        app_id=app_id,
        lang=lang.lower(),
        country=country.lower(),
        sort=args.sort,
        score=args.score,
        device=args.device,
        page_size=page_size,
        delay=delay,
        burst_every=burst_every,
        max_reviews=max_reviews,
        out=args.out,
        fresh=fresh,
    )


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def status(config: Config) -> int:
    directory = output_directory(config)
    checkpoint_path = directory / "checkpoint.json"
    app_path = directory / "app.json"
    reviews_path = directory / "reviews.jsonl"
    if not directory.exists():
        _log(f"nothing saved yet in {directory}")
        return 1
    saved = _count_lines(reviews_path)
    listed = None
    title = config.app_id
    if app_path.exists():
        app = json.loads(app_path.read_text(encoding="utf-8"))
        title = app.get("title") or title
        listed = app.get("reviews")
    _log(f"app      {title}")
    _log(f"output   {directory}")
    if listed is None:
        _log(f"reviews  {saved:,} saved")
    else:
        _log(f"reviews  {saved:,} saved · store lists {listed:,}" if isinstance(listed, int) else f"reviews  {saved:,} saved")
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        state = "finished" if checkpoint.get("finished") else "in progress"
        _log(
            f"walk     {state} · {checkpoint.get('pagesFetched', 0)} pages · "
            f"updated {checkpoint.get('updatedAt', 'unknown')}"
        )
    else:
        _log("walk     no checkpoint yet")
    return 0


def download(config: Config) -> int:
    client = PlayClient(config.lang, config.country, log=_log)
    try:
        result = run(config, client, log=_log)
    except AppNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (StoreError, DownloadError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.interrupted:
        return 130
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="playreviews",
        description=(
            "Download a Google Play app listing and its public reviews. "
            "Reviews are requested in small pages with the continuation token "
            "the Play Store website returns as a person moves through them, "
            "and the tool pauses between pages. There is no review cap unless you pass --max."
        ),
    )
    parser.add_argument("--version", action="version", version=f"playreviews {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("download", help="Save the listing, permissions, and reviews.")
    fetch.add_argument("app", help="Package name, such as com.whatsapp, or a Play Store details URL.")
    _add_query_flags(fetch)
    fetch.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Reviews per request. Default {DEFAULT_PAGE_SIZE}, the size of a store review page. Max {MAX_PAGE_SIZE}.",
    )
    fetch.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds to pause between pages, with a little jitter. Default {DEFAULT_DELAY_SECONDS}.",
    )
    fetch.add_argument(
        "--burst-every",
        type=int,
        default=10,
        help="Add a longer pause after this many pages, as if the reader stopped. 0 disables it.",
    )
    fetch.add_argument("--max", type=int, help="Stop after this many reviews are on disk. Omit it to keep going.")
    fetch.add_argument("--fresh", action="store_true", help="Delete the saved reviews and checkpoint, then start over.")

    show = sub.add_parser("status", help="Show how many reviews are saved for an app.")
    show.add_argument("app", help="Package name or Play Store details URL.")
    _add_query_flags(show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "download":
        return download(_build_config(args, downloading=True))
    if args.command == "status":
        return status(_build_config(args, downloading=False))
    parser.error(f"Unknown command {args.command}")
    return 2
