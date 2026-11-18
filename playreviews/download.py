"""Walk every review page and write each one before asking for the next.

There is no hidden cap. A run stops when the store sends no further
continuation token, when the requested maximum is already on disk, or when
the process is interrupted. The token is stored only after the page is on
disk, so running the same command again continues from that page.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from playreviews.client import StoreError
from playreviews.constants import DEVICES, SORTS

Log = Callable[[str], None]
Sleep = Callable[[float], None]


class DownloadError(Exception):
    """The saved checkpoint belongs to a different query than this run."""


@dataclass
class Config:
    app_id: str
    lang: str = "en"
    country: str = "us"
    sort: str = "newest"
    score: int | None = None
    device: str | None = None
    page_size: int = 40
    delay: float = 2.5
    burst_every: int = 10
    max_reviews: int | None = None
    out: Path = Path("downloads")
    fresh: bool = False


@dataclass
class Result:
    output_dir: Path
    saved: int
    pages: int
    finished: bool
    interrupted: bool = False


def output_directory(config: Config) -> Path:
    parts = [config.lang, config.country, config.sort]
    if config.score:
        parts.append(f"stars-{config.score}")
    if config.device:
        parts.append(config.device)
    return Path(config.out) / config.app_id / "-".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_ids(path: Path, log: Log) -> set[str]:
    found: set[str] = set()
    if not path.exists():
        return found
    skipped = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                review_id = json.loads(line).get("reviewId")
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(review_id, str):
                found.add(review_id)
    if skipped:
        log(f"warning  skipped {skipped} unreadable lines in {path.name}")
    return found


def _append_reviews(path: Path, reviews: list[dict[str, Any]]) -> None:
    blob = "".join(json.dumps(review, ensure_ascii=False) + "\n" for review in reviews)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())


def _pause(page_number: int, delay: float, burst_every: int, rng: random.Random) -> float:
    if delay <= 0:
        return 0.0
    spread = delay * 0.45
    wait = rng.uniform(max(0.2, delay - spread), delay + spread)
    if burst_every and page_number % burst_every == 0:
        wait += rng.uniform(8.0, 18.0)
    return wait


def _compatible(checkpoint: dict[str, Any], config: Config) -> bool:
    return (
        checkpoint.get("appId") == config.app_id
        and checkpoint.get("lang") == config.lang
        and checkpoint.get("country") == config.country
        and checkpoint.get("sort") == config.sort
        and checkpoint.get("score") == config.score
        and checkpoint.get("device") == config.device
    )


def _fmt(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.1f}"
    return "?" if value is None else str(value)


def run(
    config: Config,
    client: Any,
    log: Log = print,
    sleep: Sleep = time.sleep,
    rng: random.Random | None = None,
) -> Result:
    rng = rng or random.Random()
    directory = output_directory(config)
    directory.mkdir(parents=True, exist_ok=True)
    reviews_path = directory / "reviews.jsonl"
    checkpoint_path = directory / "checkpoint.json"
    app_path = directory / "app.json"
    permissions_path = directory / "permissions.json"

    if config.fresh:
        reviews_path.unlink(missing_ok=True)
        checkpoint_path.unlink(missing_ok=True)
        log("fresh    removed the previous review file and checkpoint")

    checkpoint = _read_json(checkpoint_path)
    if checkpoint and not _compatible(checkpoint, config):
        raise DownloadError(
            "A checkpoint for a different language, country, sort, score, or device "
            f"is already in {directory}. Pass --fresh to start that folder over."
        )

    log(f"output   {directory}")
    seen = _load_ids(reviews_path, log)
    if checkpoint and checkpoint.get("finished") and not config.fresh:
        log(f"done     {len(seen):,} reviews already saved, and the store had no further pages")
        return Result(directory, len(seen), int(checkpoint.get("pagesFetched") or 0), True)

    token = checkpoint.get("nextToken") if checkpoint else None
    pages = int(checkpoint.get("pagesFetched") or 0) if checkpoint else 0
    if seen:
        log(f"resume   {len(seen):,} reviews already saved")
        if token:
            log("resume   continuing from the saved continuation token")
        elif not (checkpoint and checkpoint.get("finished")):
            log("resume   no token was saved, so this walk starts again and skips reviews already on disk")

    fetched_at = _now()
    app = client.fetch_app(config.app_id, fetched_at)
    log(
        "listing  "
        f"{app.get('title') or config.app_id} · "
        f"{_fmt(app.get('score'))} · "
        f"{_fmt(app.get('ratings'))} ratings · "
        f"{_fmt(app.get('reviews'))} reviews on the store"
    )

    try:
        permissions = client.fetch_permissions(config.app_id)
        noun = "category" if len(permissions) == 1 else "categories"
        log(f"permissions  {len(permissions)} {noun}")
    except StoreError as exc:
        permissions = []
        log(f"warning  permissions were not available ({exc})")

    app["permissions"] = permissions
    _write_json(app_path, app)
    _write_json(permissions_path, permissions)

    if config.max_reviews is not None and len(seen) >= config.max_reviews:
        log(f"done     {len(seen):,} reviews already saved, which meets --max {config.max_reviews}")
        return Result(directory, len(seen), pages, False)

    sort_id = SORTS[config.sort]
    device_id = DEVICES[config.device] if config.device else None
    finished = False
    interrupted = False
    empty_pages = 0

    if config.delay > 0:
        opening = rng.uniform(0.4, min(1.6, max(config.delay, 0.4)))
        log(f"pause    {opening:.1f}s after opening the listing")
        sleep(opening)

    try:
        while True:
            request_token = token
            reviews, next_token = client.fetch_reviews(
                config.app_id,
                sort_id,
                config.page_size,
                request_token,
                config.score,
                device_id,
            )
            pages += 1
            fresh = []
            for review in reviews:
                review_id = review.get("reviewId")
                if not isinstance(review_id, str) or review_id in seen:
                    continue
                review["lang"] = config.lang
                review["country"] = config.country
                review["sort"] = config.sort
                fresh.append(review)
                seen.add(review_id)
                if config.max_reviews is not None and len(seen) >= config.max_reviews:
                    break

            if fresh:
                _append_reviews(reviews_path, fresh)

            page_ids = [
                review.get("reviewId")
                for review in reviews
                if isinstance(review.get("reviewId"), str)
            ]
            page_complete = all(review_id in seen for review_id in page_ids)
            if reviews:
                empty_pages = 0
            else:
                empty_pages += 1

            # A partial page means --max was reached before every returned
            # review was stored. Keep this page's token so the rest is not skipped.
            if not page_complete:
                token = request_token
                finished = False
            elif (
                not next_token
                or next_token == request_token
                or empty_pages >= 2
            ):
                token = None
                finished = True
            else:
                token = next_token
                finished = False

            _write_json(
                checkpoint_path,
                {
                    "appId": config.app_id,
                    "lang": config.lang,
                    "country": config.country,
                    "sort": config.sort,
                    "score": config.score,
                    "device": config.device,
                    "pageSize": config.page_size,
                    "nextToken": token,
                    "pagesFetched": pages,
                    "savedReviews": len(seen),
                    "finished": finished,
                    "updatedAt": _now(),
                },
            )
            log(
                f"reviews  page {pages}  +{len(fresh)}  saved {len(seen):,}"
                + ("  end of reviews" if finished else "")
            )

            if finished:
                break
            if config.max_reviews is not None and len(seen) >= config.max_reviews:
                log(f"paused   reached --max {config.max_reviews}; run the same command to continue")
                break

            wait = _pause(pages, config.delay, config.burst_every, rng)
            if wait:
                log(f"pause    {wait:.1f}s before the next page")
                sleep(wait)
    except KeyboardInterrupt:
        interrupted = True
        log("stopped  progress is on disk; run the same command to continue")
    except StoreError:
        log("stopped  the store paused the walk; progress is on disk, run the same command to continue")
        raise

    return Result(directory, len(seen), pages, finished, interrupted)
