"""
Stage 2: Remix — Turn raw articles into a podcast script using an LLM.

This is where the magic happens. The LLM takes a bunch of dry newsletter
articles and turns them into a lively conversation between two hosts
(or a solo monologue for 1-host mode).

Think of this as hiring a talented scriptwriter who reads all the day's
news and writes a podcast script that sounds natural and engaging.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Template

# Allow importing utils from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from utils import get_data_dir, get_skill_dir, load_config, load_env, read_state, setup_logging


def generate_script(articles, config, logger=None):
    """
    Takes a list of articles and generates a podcast script using an LLM.

    How it works:
    1. Picks the right prompt template (1-host or 2-host)
    2. Fills in the template with today's articles and show settings
    3. Sends it to the LLM (Claude or GPT) and gets back a conversation script
    4. Validates the response is properly formatted JSON
    5. Saves the script to disk (so you can retry TTS if it fails later)

    Args:
        articles: List of article dicts from fetch.py
        config: Your parsed config.yaml
        logger: Logger instance

    Returns:
        List of script segments: [{"speaker": "A", "text": "..."}, ...]
    """
    if logger is None:
        logger = setup_logging()

    data_dir = get_data_dir()
    skill_dir = get_skill_dir()

    # Pick the right prompt template based on number of hosts
    num_hosts = config.get("hosts", 2)
    template_name = f"prompt_{num_hosts}host.md"
    template_path = skill_dir / "templates" / template_name

    if not template_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {template_path}")

    logger.info(f"Using {num_hosts}-host prompt template")

    # Read and render the template with our variables
    with open(template_path, "r") as f:
        template = Template(f.read())

    # Format articles for the prompt — give the LLM a clean summary of each
    articles_text = "\n\n".join(
        f"### Article {i+1}: {a['title']}\n"
        f"**Source:** {a['source_name']} | **Author:** {a['author']} | **Date:** {a['published']}\n\n"
        f"{a['content']}"
        for i, a in enumerate(articles)
    )

    prompt = template.render(
        articles=articles_text,
        show_name=config.get("show_name", "My Daily Digest"),
        tone=config.get("tone", "casual and conversational"),
        length_minutes=config.get("length_minutes", 10),
        date=datetime.now().strftime("%B %d, %Y"),
    )

    # Call the LLM
    llm_config = config.get("llm", {})
    provider = llm_config.get("provider", "anthropic")
    model = llm_config.get("model", "claude-sonnet-4-6")

    # Get the API key from the environment variable specified in config
    api_key_env = llm_config.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"API key not found in environment variable '{api_key_env}'.\n"
            f"Make sure it's set in your .env file at ~/.claude/personalized-podcast/.env"
        )

    logger.info(f"Generating script with {provider} ({model})...")

    # Try up to 2 times — if the first response is malformed JSON, retry with a stricter prompt
    for attempt in range(2):
        try:
            if attempt == 1:
                logger.warning("First attempt returned malformed JSON. Retrying with stricter prompt...")
                prompt += (
                    "\n\nCRITICAL: Your previous response was not valid JSON. "
                    "You MUST respond with ONLY a valid JSON array. "
                    "No markdown, no code blocks, no explanation. Just the raw JSON array starting with [ and ending with ]."
                )

            raw_response = _call_llm(provider, model, api_key, prompt, logger)
            script = _parse_script(raw_response, num_hosts, logger)

            # Save the script to disk for potential TTS retry later
            output_path = data_dir / "scripts_output" / f"{datetime.now().strftime('%Y-%m-%d')}.json"
            with open(output_path, "w") as f:
                json.dump(script, f, indent=2)
            logger.info(f"Script saved to {output_path}")

            # Log some stats about the generated script
            total_words = sum(len(s["text"].split()) for s in script)
            logger.info(f"Generated script: {len(script)} segments, ~{total_words} words")

            return script

        except (json.JSONDecodeError, ValueError) as e:
            if attempt == 0:
                logger.warning(f"Script parsing failed: {e}")
                # Save the raw response for debugging
                debug_path = data_dir / "scripts_output" / f"{datetime.now().strftime('%Y-%m-%d')}_raw.txt"
                with open(debug_path, "w") as f:
                    f.write(raw_response)
                logger.info(f"Raw LLM response saved to {debug_path} for debugging")
            else:
                raise ValueError(
                    f"LLM returned malformed JSON after 2 attempts.\n"
                    f"Raw response saved for debugging. Error: {e}"
                )


def _call_llm(provider, model, api_key, prompt, logger):
    """
    Calls the LLM API and returns the raw text response.

    Supports two providers:
    - "anthropic": Uses the Anthropic SDK (Claude models)
    - "openai": Uses the OpenAI SDK (GPT models)
    """
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    elif provider == "openai":
        import openai
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    else:
        raise ValueError(f"Unknown LLM provider: '{provider}'. Use 'anthropic' or 'openai'.")


def _parse_script(raw_response, num_hosts, logger):
    """
    Parses the LLM's response into a clean list of script segments.

    The LLM should return a JSON array, but sometimes it wraps it in
    markdown code blocks or adds extra text. This function handles
    those common quirks.
    """
    text = raw_response.strip()

    # Strip markdown code block wrappers if present
    # (LLMs sometimes wrap JSON in ```json ... ```)
    if text.startswith("```"):
        # Remove first line (```json or ```) and last line (```)
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    # Try to find JSON array in the response if there's extra text
    if not text.startswith("["):
        # Look for the first [ and last ] to extract the JSON array
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end + 1]

    # Parse the JSON
    script = json.loads(text)

    # Validate structure
    if not isinstance(script, list):
        raise ValueError(f"Expected JSON array, got {type(script).__name__}")

    valid_speakers = {"A"} if num_hosts == 1 else {"A", "B"}

    for i, segment in enumerate(script):
        if not isinstance(segment, dict):
            raise ValueError(f"Segment {i} is not an object: {segment}")
        if "speaker" not in segment or "text" not in segment:
            raise ValueError(f"Segment {i} missing 'speaker' or 'text': {segment}")
        if segment["speaker"] not in valid_speakers:
            raise ValueError(
                f"Segment {i} has invalid speaker '{segment['speaker']}'. "
                f"Expected one of: {valid_speakers}"
            )

    return script


def load_saved_script(date_str=None):
    """
    Loads a previously saved script from disk.
    Used when retrying TTS (--from-stage speak).
    """
    data_dir = get_data_dir()
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    script_path = data_dir / "scripts_output" / f"{date_str}.json"
    if not script_path.exists():
        raise FileNotFoundError(f"No saved script found for {date_str} at {script_path}")

    with open(script_path, "r") as f:
        return json.load(f)


if __name__ == "__main__":
    """
    Run this directly to test script generation:
      python remix.py
    """
    logger = setup_logging()
    load_env()
    config = load_config()
    state = read_state()

    # Import fetch to get fresh articles
    from fetch import fetch_feeds
    articles = fetch_feeds(config, state, logger)

    if not articles:
        logger.info("No new articles found. Nothing to remix.")
        sys.exit(0)

    script = generate_script(articles, config, logger)

    print(f"\n{'='*60}")
    print(f"Generated script ({len(script)} segments):")
    print(f"{'='*60}")
    for segment in script:
        speaker = "Alex" if segment["speaker"] == "A" else "Sam"
        print(f"\n[{speaker}]: {segment['text']}")
