"""Searches LinkedIn posts through the Apify actor harvestapi/linkedin-post-search."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any, Iterable

ACTOR_ID = "harvestapi/linkedin-post-search"
PRICE_PER_POST_USD = 0.002  # Apify free-tier price per post for this actor.
POSTED_LIMITS = ["24h", "week", "month"]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def parse_hashtags(text: str) -> list[str]:
    """'#hiring, deliverymanager #QA' -> ['#hiring', '#deliverymanager', '#qa']"""
    tags: list[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        tag = raw.strip().lstrip("#").strip()
        if not tag:
            continue
        tag = "#" + tag.lower()
        if tag not in tags:
            tags.append(tag)
    return tags


def build_queries(hashtags: str, keywords: str, location: str = "") -> list[str]:
    """One LinkedIn search per keyword line, each combined with the hashtags and location."""
    tag_part = " ".join(parse_hashtags(hashtags))
    loc = (location or "").strip()
    lines = [line.strip() for line in (keywords or "").splitlines() if line.strip()]
    queries: list[str] = []
    for kw in lines or [""]:
        query = " ".join(p for p in (tag_part, kw, loc) if p)
        if query and query not in queries:
            queries.append(query)
    return queries


def extract_emails(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_RE.findall(text or ""):
        email = match.strip(".-_")
        key = email.lower()
        if key not in seen:
            seen.add(key)
            found.append(email)
    return found


def has_any_hashtag(text: str, hashtags: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    tags = list(hashtags)
    if not tags:
        return True
    return any(tag.lower() in lowered for tag in tags)


def normalize_post(item: dict[str, Any]) -> dict[str, Any] | None:
    """Maps one actor dataset item to the fields the app stores. Returns None for empty posts."""
    content = (item.get("content") or "").strip()
    repost = item.get("repost") or {}
    repost_content = (repost.get("content") or "").strip() if isinstance(repost, dict) else ""
    if repost_content and repost_content not in content:
        content = f"{content}\n\n[Reposted]\n{repost_content}".strip()
    if not content:
        return None

    url = item.get("linkedinUrl") or item.get("shareLinkedinUrl") or ""
    raw_id = item.get("id") or item.get("entityId")
    post_id = str(raw_id) if raw_id else hashlib.sha1((url or content).encode("utf-8")).hexdigest()

    author = item.get("author") or {}
    posted = item.get("postedAt") or {}
    query = item.get("query") or {}
    return {
        "post_id": post_id,
        "url": url,
        "text": content,
        "author_name": author.get("name") or "",
        "author_headline": author.get("info") or "",
        "author_url": author.get("linkedinUrl") or "",
        "posted_at": posted.get("date") or "",
        "posted_ago": posted.get("postedAgoText") or posted.get("postedAgoShort") or "",
        "query": query.get("search") or "",
        "emails": extract_emails(content),
    }


def scrape_posts(
    token: str,
    queries: list[str],
    *,
    max_posts: int,
    posted_limit: str = "week",
    sort_by: str = "date",
    client: Any = None,
) -> list[dict[str, Any]]:
    """Runs the actor and returns raw dataset items."""
    if client is None:
        from apify_client import ApifyClient

        client = ApifyClient(token)
    run_input = {
        "searchQueries": queries,
        "maxPosts": int(max_posts),
        "postedLimit": posted_limit,
        "sortBy": sort_by,
    }
    run = client.actor(ACTOR_ID).call(run_input=run_input, wait_duration=timedelta(minutes=20))
    if run is None:
        raise RuntimeError("The Apify run did not start.")
    status = _field(run, "status")
    status = str(getattr(status, "value", status) or "")
    if status != "SUCCEEDED":
        raise RuntimeError(f"The Apify run ended with status {status or 'unknown'}.")
    dataset_id = _field(run, "default_dataset_id") or _field(run, "defaultDatasetId")
    return list(client.dataset(dataset_id).iterate_items())


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def dedupe_posts(posts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for post in posts:
        if post["post_id"] in seen:
            continue
        seen.add(post["post_id"])
        unique.append(post)
    return unique
