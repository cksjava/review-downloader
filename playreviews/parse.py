"""Turn Play Store page and batchexecute payloads into plain records.

The website embeds the listing in AF_initDataCallback blobs and returns
reviews from the oCPfdb RPC as length-prefixed JSON wrapped in an XSSI
prefix. Field positions follow that payload, with each lookup guarded so a
missing slot becomes null instead of failing the download.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any

from playreviews.constants import DEVICE_LABELS

SCRIPT = re.compile(r">AF_initDataCallback[\s\S]*?</script")
SCRIPT_KEY = re.compile(r"(ds:\d+)")
SCRIPT_VALUE = re.compile(r"data:([\s\S]*?), sideChannel: \{\}\}\);</")
TAG = re.compile(r"<[^>]+>")
BREAK = re.compile(r"<br\s*/?>|</p>", re.IGNORECASE)


class ParseError(Exception):
    """A Play Store body was not the payload this client knows how to read."""


def dig(node: Any, path: list[int]) -> Any:
    current = node
    for key in path:
        if not isinstance(current, list) or key < 0 or key >= len(current):
            return None
        current = current[key]
    return current


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = unescape(value)
    text = BREAK.sub("\n", text)
    text = TAG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    cleaned = text.strip()
    return cleaned or None


def iso_from_unix(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unix_of(node: Any) -> int | None:
    if isinstance(node, list) and node and isinstance(node[0], (int, float)):
        return int(node[0])
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return int(node)
    return None


def first_http(node: Any) -> str | None:
    if isinstance(node, str) and node.startswith("http"):
        return node
    if isinstance(node, list):
        for child in node:
            found = first_http(child)
            if found:
                return found
    return None


def collect_urls(node: Any, found: list[str]) -> None:
    if isinstance(node, str) and node.startswith("http"):
        found.append(node)
    elif isinstance(node, list):
        for child in node:
            collect_urls(child, found)


def find_email(node: Any) -> str | None:
    if isinstance(node, str) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", node):
        return node
    if isinstance(node, list):
        for child in node:
            found = find_email(child)
            if found:
                return found
    return None


def find_dev_path(node: Any) -> str | None:
    if isinstance(node, str) and "/store/apps/dev" in node:
        return node
    if isinstance(node, list):
        for child in node:
            found = find_dev_path(child)
            if found:
                return found
    return None


def plain_strings(node: Any, found: list[str]) -> None:
    if isinstance(node, str):
        text = node.strip()
        if text and not text.startswith("http") and not text.startswith("/") and "@" not in text:
            found.append(text)
    elif isinstance(node, list):
        for child in node:
            plain_strings(child, found)


def parse_dataset(html: str) -> dict[str, Any]:
    dataset: dict[str, Any] = {}
    for match in SCRIPT.findall(html):
        keys = SCRIPT_KEY.findall(match)
        values = SCRIPT_VALUE.findall(match)
        if not keys or not values:
            continue
        try:
            dataset[keys[0]] = json.loads(values[0])
        except json.JSONDecodeError:
            continue
    return dataset


def app_node(dataset: dict[str, Any]) -> list[Any] | None:
    node = dig(dataset.get("ds:5"), [1, 2])
    if isinstance(node, list) and node and isinstance(node[0], list):
        return node
    return None


def _histogram(node: list[Any]) -> dict[str, int | None]:
    bucket = dig(node, [51, 1])
    histogram: dict[str, int | None] = {}
    for stars in range(1, 6):
        entry = bucket[stars] if isinstance(bucket, list) and stars < len(bucket) else None
        count = entry[1] if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], int) else None
        histogram[str(stars)] = count
    return histogram


def _device_scores(node: list[Any]) -> list[dict[str, Any]]:
    block = node[150] if len(node) > 150 else None
    if not isinstance(block, list) or len(block) < 2 or not isinstance(block[1], list):
        return []
    scores = []
    for entry in block[1]:
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], int):
            continue
        score = dig(entry, [1, 1])
        if not isinstance(score, (int, float)):
            continue
        device_id = entry[0]
        scores.append(
            {
                "device": DEVICE_LABELS.get(device_id, f"device-{device_id}"),
                "deviceId": device_id,
                "score": score,
            }
        )
    return scores


def _price(node: list[Any]) -> tuple[float, str | None, bool]:
    found: dict[str, Any] = {}

    def walk(current: Any) -> None:
        if "amount" in found:
            return
        if (
            isinstance(current, list)
            and len(current) >= 2
            and isinstance(current[0], (int, float))
            and not isinstance(current[0], bool)
            and isinstance(current[1], str)
            and re.fullmatch(r"[A-Z]{3}", current[1])
        ):
            found["amount"] = current[0]
            found["currency"] = current[1]
            return
        if isinstance(current, list):
            for child in current:
                walk(child)

    if len(node) > 57:
        walk(node[57])
    if "amount" not in found:
        return 0.0, None, True
    amount = found["amount"]
    price = amount / 1_000_000 if isinstance(amount, int) and amount >= 10_000 else float(amount)
    return price, found["currency"], price == 0


def _screenshots(node: list[Any]) -> list[dict[str, Any]]:
    # The gallery is either a list of images or a list wrapping that list,
    # and each image is [*, *, [width, height], [*, *, url]].
    container = node[78] if len(node) > 78 else None
    shots: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(item: Any) -> None:
        if not isinstance(item, list):
            return
        size = item[2] if len(item) > 2 else None
        url = dig(item, [3, 2])
        if (
            isinstance(url, str)
            and isinstance(size, list)
            and len(size) >= 2
            and isinstance(size[0], int)
            and isinstance(size[1], int)
        ):
            if url not in seen:
                seen.add(url)
                shots.append({"url": url, "width": size[0], "height": size[1]})
            return
        for child in item:
            walk(child)

    walk(container)
    return shots


def _categories(node: list[Any]) -> list[dict[str, str | None]]:
    container = node[79] if len(node) > 79 else None
    categories = []
    if not isinstance(container, list):
        return categories
    for group in container:
        entry = group[0] if isinstance(group, list) and group else None
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
            continue
        category_id = entry[2] if len(entry) > 2 and isinstance(entry[2], str) else None
        categories.append({"name": entry[0], "id": category_id})
    return categories


def _data_safety(node: list[Any]) -> list[dict[str, str | None]]:
    block = node[136] if len(node) > 136 else None
    entries = block[1] if isinstance(block, list) and len(block) > 1 else None
    if not isinstance(entries, list):
        return []
    parsed = []
    for entry in entries:
        if not isinstance(entry, list):
            continue
        titles = [item for item in entry if isinstance(item, str) and not item.startswith("http")]
        detail = None
        for item in entry:
            if isinstance(item, list):
                for child in item:
                    if isinstance(child, str) and not child.startswith("http"):
                        detail = html_to_text(child)
        if titles:
            parsed.append({"title": titles[0], "detail": detail})
    return parsed


def _version(node: list[Any]) -> str | None:
    block = node[140] if len(node) > 140 else None
    found: list[str] = []

    def walk(current: Any) -> None:
        if isinstance(current, str) and re.fullmatch(r"\d+(?:\.\d+)+", current):
            found.append(current)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(block)
    return found[0] if found else None


def _address(node: Any) -> str | None:
    parts: list[str] = []
    plain_strings(node, parts)
    if not parts:
        return None
    return ", ".join(parts)


def parse_listing(
    node: list[Any],
    app_id: str,
    url: str,
    lang: str,
    country: str,
    fetched_at: str,
) -> dict[str, Any]:
    description_html = dig(node, [72, 0, 1])
    summary_html = dig(node, [73, 0, 1])
    recent_html = dig(node, [144, 1, 1])
    if not isinstance(description_html, str):
        description_html = None
    if not isinstance(summary_html, str):
        summary_html = None
    if not isinstance(recent_html, str):
        recent_html = None

    price, currency, free = _price(node)
    categories = _categories(node)
    in_app = dig(node, [19, 0])
    dev_path = find_dev_path(node[68] if len(node) > 68 else None)
    video_urls: list[str] = []
    if len(node) > 100:
        collect_urls(node[100], video_urls)
    ads = node[48] if len(node) > 48 else None

    return {
        "appId": app_id,
        "url": url,
        "title": dig(node, [0, 0]),
        "summary": html_to_text(summary_html),
        "description": html_to_text(description_html),
        "descriptionHtml": description_html,
        "installs": dig(node, [13, 0]),
        "minInstalls": dig(node, [13, 1]),
        "realInstalls": dig(node, [13, 2]),
        "score": dig(node, [51, 0, 1]),
        "ratings": dig(node, [51, 2, 1]),
        "reviews": dig(node, [51, 3, 1]),
        "histogram": _histogram(node),
        "deviceScores": _device_scores(node),
        "price": price,
        "currency": currency,
        "free": free,
        "inAppPurchases": in_app if isinstance(in_app, str) else None,
        "developer": dig(node, [68, 0]),
        "developerId": dig(node, [68, 2]),
        "developerUrl": f"https://play.google.com{dev_path}" if dev_path else None,
        "developerEmail": find_email(node[69] if len(node) > 69 else None),
        "developerWebsite": first_http(dig(node, [69, 0])),
        "developerAddress": _address(dig(node, [69, 2])),
        "privacyPolicy": first_http(node[99] if len(node) > 99 else None),
        "genre": categories[0]["name"] if categories else None,
        "genreId": categories[0]["id"] if categories else None,
        "categories": categories,
        "icon": dig(node, [95, 0, 3, 2]),
        "headerImage": dig(node, [96, 0, 3, 2]),
        "screenshots": _screenshots(node),
        "video": video_urls[0] if video_urls else None,
        "videoImage": video_urls[1] if len(video_urls) > 1 else None,
        "contentRating": dig(node, [9, 0]),
        "containsAds": isinstance(ads, list) and len(ads) > 0,
        "updated": dig(node, [145, 0, 0]),
        "updatedUnix": dig(node, [145, 0, 1, 0]),
        "recentChanges": html_to_text(recent_html),
        "recentChangesHtml": recent_html,
        "version": _version(node),
        "dataSafety": _data_safety(node),
        "lang": lang,
        "country": country,
        "fetchedAt": fetched_at,
    }


def parse_permissions(payload: Any) -> list[dict[str, Any]]:
    """Accept the xdSrCf payload, which mixes named groups and a bare list."""
    categories: list[dict[str, Any]] = []
    uncategorized: list[str] = []

    def permission_name(entry: Any) -> str | None:
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and entry[0] is None
            and isinstance(entry[1], str)
            and not entry[1].startswith("http")
        ):
            return entry[1]
        return None

    def consume(node: Any) -> None:
        if not isinstance(node, list) or not node:
            return
        first = node[0]
        if isinstance(first, str):
            names: list[str] = []
            for child in node:
                if not isinstance(child, list):
                    continue
                for entry in child:
                    name = permission_name(entry)
                    if name and name not in names:
                        names.append(name)
            categories.append({"category": first, "permissions": names})
            return
        if isinstance(first, list) and first and isinstance(first[0], str):
            for group in node:
                consume(group)
            return
        if permission_name(first):
            for entry in node:
                name = permission_name(entry)
                if name and name not in uncategorized:
                    uncategorized.append(name)
            return
        for child in node:
            consume(child)

    consume(payload)
    if uncategorized:
        categories.append({"category": "Uncategorized", "permissions": uncategorized})
    return categories


def _criteria(item: list[Any]) -> list[dict[str, Any]] | None:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) == 2
            and isinstance(node[0], str)
            and node[0].startswith("vaf_")
            and isinstance(node[1], list)
        ):
            rating = node[1][0] if node[1] else None
            found.append({"id": node[0], "rating": rating})
            return
        if isinstance(node, list):
            for child in node:
                walk(child)

    for index, part in enumerate(item):
        # The profile blocks repeat the author and are not review criteria.
        if index in (1, 9):
            continue
        walk(part)
    return found or None


def parse_review(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, list) or not item or not isinstance(item[0], str):
        return None
    created = unix_of(item[5] if len(item) > 5 else None)
    reply = None
    reply_node = item[7] if len(item) > 7 else None
    if isinstance(reply_node, list) and len(reply_node) >= 2 and isinstance(reply_node[1], str):
        replied = unix_of(reply_node[2] if len(reply_node) > 2 else None)
        reply = {
            "developer": reply_node[0] if isinstance(reply_node[0], str) else None,
            "content": reply_node[1],
            "at": iso_from_unix(replied),
            "atUnix": replied,
        }
    title = item[3] if len(item) > 3 and isinstance(item[3], str) else None
    content = item[4] if len(item) > 4 and isinstance(item[4], str) else None
    score = item[2] if len(item) > 2 and isinstance(item[2], (int, float)) else None
    thumbs = item[6] if len(item) > 6 and isinstance(item[6], int) else 0
    version = item[10] if len(item) > 10 and isinstance(item[10], str) else None
    record: dict[str, Any] = {
        "reviewId": item[0],
        "userName": dig(item, [1, 0]),
        "userImage": first_http(item[1] if len(item) > 1 else None),
        "score": score,
        "title": title,
        "content": content,
        "thumbsUpCount": thumbs,
        "appVersion": version,
        "at": iso_from_unix(created),
        "atUnix": created,
        "reply": reply,
        "criteria": _criteria(item),
    }
    extras = {}
    for index in (8, 11, 12, 13, 14, 15, 16):
        if index < len(item) and item[index] is not None:
            extras[str(index)] = item[index]
    if extras:
        record["extras"] = extras
    return record


def parse_rpc(raw: str, rpc_id: str) -> Any:
    """Return the decoded inner payload for one batchexecute RPC id."""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, list):
            continue
        for entry in obj:
            if (
                isinstance(entry, list)
                and len(entry) > 2
                and entry[1] == rpc_id
                and isinstance(entry[2], str)
            ):
                try:
                    return json.loads(entry[2])
                except json.JSONDecodeError as exc:
                    raise ParseError(f"Could not decode the {rpc_id} payload.") from exc
    raise ParseError(f"The response did not include the {rpc_id} call.")


def parse_review_response(raw: str) -> tuple[list[dict[str, Any]], str | None]:
    payload = parse_rpc(raw, "oCPfdb")
    if not isinstance(payload, list):
        raise ParseError("The review payload was not a list.")
    raw_reviews = payload[0] if payload else []
    if raw_reviews is None:
        raw_reviews = []
    if not isinstance(raw_reviews, list):
        raise ParseError("The review list had an unexpected shape.")
    reviews = []
    for item in raw_reviews:
        parsed = parse_review(item)
        if parsed:
            reviews.append(parsed)
    token = None
    slot = payload[1] if len(payload) > 1 else None
    if isinstance(slot, list) and len(slot) > 1 and isinstance(slot[1], str) and slot[1]:
        token = slot[1]
    return reviews, token
