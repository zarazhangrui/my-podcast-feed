#!/usr/bin/env python3
"""
Pipeline Orchestrator — Runs all stages of the podcast generation pipeline.

This is the "conductor" that coordinates the whole orchestra:
  Fetch → Remix → Speak → Publish

You can run the full pipeline, or restart from a specific stage if
something failed partway through (e.g., TTS quota ran out but the
script was already generated — just retry from "speak").

Usage:
  python run_pipeline.py                         # Full pipeline
  python run_pipeline.py --from-stage speak      # Resume from TTS
  python run_pipeline.py --from-stage speak --date 2026-03-14  # Retry specific day
  python run_pipeline.py --config-path /path/to/config.yaml    # Custom config
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from utils import get_data_dir, load_config, load_env, read_state, setup_logging, write_state


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="Run the personalized podcast generation pipeline"
    )
    parser.add_argument(
        "--from-stage",
        choices=["fetch", "remix", "speak", "publish"],
        default="fetch",
        help="Stage to start from (default: fetch = full pipeline)",
    )
    parser.add_argument(
        "--date",
        help="Date to use for loading saved data (YYYY-MM-DD). "
             "Used with --from-stage to retry a specific day's episode.",
    )
    parser.add_argument(
        "--config-path",
        help="Path to config.yaml (default: ~/.claude/personalized-podcast/config.yaml)",
    )
    args = parser.parse_args()

    # Initialize everything
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Personalized Podcast Pipeline")
    logger.info("=" * 60)

    try:
        load_env()
    except FileNotFoundError as e:
        if args.from_stage == "fetch":
            # .env not needed for fetch (no API calls), but warn
            logger.warning(f"Note: {e}")
        else:
            raise

    config = load_config(args.config_path)
    state = read_state()
    data_dir = get_data_dir()

    # Track what stage we're on and results from each stage
    articles = None
    script = None
    mp3_path = None
    feed_url = None

    stages = ["fetch", "remix", "speak", "publish"]
    start_index = stages.index(args.from_stage)

    logger.info(f"Starting from stage: {args.from_stage}")
    if args.date:
        logger.info(f"Using date: {args.date}")

    # =========================================================
    # Stage 1: FETCH — Pull new articles from RSS feeds
    # =========================================================
    if start_index <= 0:
        logger.info("\n--- Stage 1: FETCH ---")
        from fetch import fetch_feeds

        articles = fetch_feeds(config, state, logger)

        if not articles:
            logger.info("No new articles found. Skipping episode generation.")
            # Update state so we don't re-check the same time window
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            write_state(state)
            logger.info("Pipeline complete (no new content)")
            return 0

        # Save fetched articles for potential retry
        articles_path = data_dir / "scripts_output" / f"{datetime.now().strftime('%Y-%m-%d')}_articles.json"
        with open(articles_path, "w") as f:
            json.dump(articles, f, indent=2)
        logger.info(f"Saved {len(articles)} articles to {articles_path}")

    # =========================================================
    # Stage 2: REMIX — Generate podcast script with LLM
    # =========================================================
    if start_index <= 1:
        logger.info("\n--- Stage 2: REMIX ---")
        from remix import generate_script, load_saved_script

        if articles is None:
            # Loading saved articles from a previous fetch
            date_str = args.date or datetime.now().strftime("%Y-%m-%d")
            articles_path = data_dir / "scripts_output" / f"{date_str}_articles.json"
            if articles_path.exists():
                with open(articles_path, "r") as f:
                    articles = json.load(f)
                logger.info(f"Loaded {len(articles)} saved articles from {articles_path}")
            else:
                raise FileNotFoundError(
                    f"No saved articles found for {date_str}. "
                    f"Run the full pipeline (without --from-stage) to fetch articles first."
                )

        script = generate_script(articles, config, logger)

    # =========================================================
    # Stage 3: SPEAK — Convert script to audio via TTS
    # =========================================================
    if start_index <= 2:
        logger.info("\n--- Stage 3: SPEAK ---")
        from speak import generate_audio

        if script is None:
            # Load a previously saved script
            from remix import load_saved_script
            date_str = args.date or datetime.now().strftime("%Y-%m-%d")
            script = load_saved_script(date_str)
            logger.info(f"Loaded saved script for {date_str} ({len(script)} segments)")

        mp3_path = generate_audio(script, config, logger)

    # =========================================================
    # Stage 4: PUBLISH — Push to GitHub Pages
    # =========================================================
    if start_index <= 3:
        logger.info("\n--- Stage 4: PUBLISH ---")
        from publish import publish_episode

        if mp3_path is None:
            # Find the latest MP3 in episodes directory
            episodes_dir = data_dir / "episodes"
            mp3_files = sorted(episodes_dir.glob("*.mp3"))
            if not mp3_files:
                raise FileNotFoundError(
                    "No MP3 files found. Run the speak stage first."
                )
            mp3_path = mp3_files[-1]
            logger.info(f"Using latest MP3: {mp3_path.name}")

        feed_url = publish_episode(mp3_path, config, logger)

    # =========================================================
    # Update state
    # =========================================================
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if articles:
        # Track which articles we processed so we don't repeat them
        new_ids = [a["id"] for a in articles]
        existing_ids = state.get("processed_ids", [])
        # Keep last 500 IDs to prevent the list from growing forever
        state["processed_ids"] = (new_ids + existing_ids)[:500]
    write_state(state)

    # =========================================================
    # Summary
    # =========================================================
    logger.info("\n" + "=" * 60)
    logger.info("Pipeline complete!")
    if articles:
        logger.info(f"  Articles processed: {len(articles)}")
    if mp3_path:
        logger.info(f"  Episode file: {mp3_path}")
    if feed_url:
        logger.info(f"  Feed URL: {feed_url}")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code or 0)
    except Exception as e:
        # Make sure errors are logged, not just printed
        import logging
        logger = logging.getLogger("personalized-podcast")
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
