"""
Stage 1: Fetch — Pull latest articles from RSS feeds.

This is the first step in the podcast pipeline. It visits each RSS feed URL
in your config, grabs any new articles since the last run, and packages them
up as a list of article dictionaries for the next stage to work with.

Think of it like a newspaper delivery person collecting today's papers from
multiple newsstands before bringing them to the editor.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import mktime

import feedparser

# Allow importing utils from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from utils import get_data_dir, load_config, load_env, read_state, setup_logging


def fetch_feeds(config, state, logger=None):
    """
    Fetches new articles from all RSS feeds in the config.

    How it works:
    1. Looks at each RSS URL in your config
    2. Pulls down the feed (like visiting a website)
    3. Filters out articles you've already seen (using timestamps)
    4. Packages each new article into a clean dictionary
    5. Returns the full list

    If one feed is down, it logs a warning and keeps going with the others.
    Only raises an error if ALL feeds fail (nothing to work with).

    Args:
        config: Your parsed config.yaml as a dictionary
        state: The pipeline state (tracks what was last processed)
        logger: Logger instance for recording what happens

    Returns:
        List of article dictionaries, each containing:
        - id: Unique identifier for the article
        - title: Article headline
        - author: Who wrote it
        - published: When it was published (ISO format string)
        - content: The full text (or summary if full text isn't available)
        - source_url: Link to the original article
        - source_name: Name of the feed it came from
    """
    if logger is None:
        logger = setup_logging()

    # Figure out the cutoff time — only grab articles newer than this
    if state.get("last_run"):
        # Use the last run time as the cutoff
        cutoff = datetime.fromisoformat(state["last_run"])
        logger.info(f"Fetching articles newer than {cutoff.isoformat()}")
    else:
        # First run ever — grab the last 24 hours of content
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        logger.info("First run — fetching articles from the last 24 hours")

    # Make sure cutoff is timezone-aware (has UTC info)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    rss_urls = config.get("sources", {}).get("rss", [])
    if not rss_urls:
        raise RuntimeError("No RSS feed URLs configured in sources.rss")

    all_articles = []
    feed_errors = []
    processed_ids = set(state.get("processed_ids", []))

    for url in rss_urls:
        logger.info(f"Fetching feed: {url}")
        try:
            # feedparser handles all the RSS/Atom format differences for us
            feed = feedparser.parse(url)

            # Check if the feed parsed successfully
            if feed.bozo and not feed.entries:
                # "bozo" is feedparser's way of saying "something was weird"
                raise Exception(f"Feed parse error: {feed.bozo_exception}")

            feed_name = feed.feed.get("title", url)
            new_count = 0

            for entry in feed.entries:
                # Create a unique ID for this article
                article_id = entry.get("id", entry.get("link", entry.get("title", "")))

                # Skip articles we've already processed
                if article_id in processed_ids:
                    continue

                # Parse the publish date
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                # Skip articles older than our cutoff
                if published and published < cutoff:
                    continue

                # Extract the content — prefer full content, fall back to summary
                content = ""
                if hasattr(entry, "content") and entry.content:
                    # content is a list of content objects; grab the first one
                    content = entry.content[0].get("value", "")
                elif hasattr(entry, "summary"):
                    content = entry.summary or ""

                # Strip HTML tags for cleaner text (basic approach)
                # We keep it simple — the LLM can handle some HTML remnants
                import re
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()

                article = {
                    "id": article_id,
                    "title": entry.get("title", "Untitled"),
                    "author": entry.get("author", feed_name),
                    "published": published.isoformat() if published else datetime.now(timezone.utc).isoformat(),
                    "content": content[:5000],  # Cap at 5000 chars to keep LLM context manageable
                    "source_url": entry.get("link", ""),
                    "source_name": feed_name,
                }
                all_articles.append(article)
                new_count += 1

            logger.info(f"  Found {new_count} new articles from '{feed_name}'")

        except Exception as e:
            logger.warning(f"  Failed to fetch {url}: {e}")
            feed_errors.append({"url": url, "error": str(e)})

    # If every single feed failed, that's a hard stop
    if len(feed_errors) == len(rss_urls):
        raise RuntimeError(
            f"All {len(rss_urls)} RSS feeds failed to fetch. Errors:\n"
            + "\n".join(f"  - {e['url']}: {e['error']}" for e in feed_errors)
        )

    # Sort articles by publish date (newest first)
    all_articles.sort(key=lambda a: a["published"], reverse=True)

    logger.info(f"Total: {len(all_articles)} new articles from {len(rss_urls) - len(feed_errors)} feeds")

    return all_articles


if __name__ == "__main__":
    """
    Run this directly to test fetching:
      python fetch.py
    """
    logger = setup_logging()
    try:
        load_env()
    except FileNotFoundError:
        pass  # .env not needed for fetch, only for API calls

    config = load_config()
    state = read_state()
    articles = fetch_feeds(config, state, logger)

    print(f"\n{'='*60}")
    print(f"Found {len(articles)} new articles:")
    print(f"{'='*60}")
    for i, article in enumerate(articles, 1):
        print(f"\n{i}. [{article['source_name']}] {article['title']}")
        print(f"   By: {article['author']} | {article['published']}")
        print(f"   Content preview: {article['content'][:200]}...")
