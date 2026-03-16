"""
Stage 3: Speak — Convert the podcast script into audio using ElevenLabs TTS.

This stage takes the conversation script (a list of speaker segments) and
turns each line into speech using ElevenLabs' text-to-speech API. Each host
gets their own voice, and all the audio chunks get stitched together into
a single MP3 file.

Think of it like a recording studio session: each host reads their lines,
and the sound engineer edits them together into one smooth episode.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Allow importing utils from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from utils import get_data_dir, load_config, load_env, setup_logging


def check_ffmpeg():
    """
    Checks that ffmpeg is installed (required by pydub for audio processing).
    Raises a helpful error if it's missing, with install instructions.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FileNotFoundError()
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg is not installed but is required for audio processing.\n\n"
            "To install it:\n"
            "  macOS:   brew install ffmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  Windows: Download from https://ffmpeg.org/download.html\n\n"
            "After installing, run the pipeline again."
        )


def generate_audio(script_segments, config, logger=None):
    """
    Converts script segments into a single MP3 file.

    How it works:
    1. Checks ffmpeg is installed (needed for audio processing)
    2. For each line in the script, calls ElevenLabs with the right voice
    3. Collects all the audio chunks
    4. Stitches them together with brief pauses between speakers
    5. Adds a gentle fade-in at the start and fade-out at the end
    6. Saves the final MP3 file

    Args:
        script_segments: List of {"speaker": "A"|"B", "text": "..."} dicts
        config: Your parsed config.yaml
        logger: Logger instance

    Returns:
        Path to the generated MP3 file
    """
    if logger is None:
        logger = setup_logging()

    # Step 1: Make sure ffmpeg is available
    check_ffmpeg()

    # Import audio libraries (pydub needs ffmpeg to work)
    from pydub import AudioSegment

    data_dir = get_data_dir()
    tts_config = config.get("tts", {})

    # Get ElevenLabs API key
    api_key_env = tts_config.get("api_key_env", "ELEVENLABS_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"ElevenLabs API key not found in environment variable '{api_key_env}'.\n"
            f"Make sure it's set in your .env file at ~/.claude/personalized-podcast/.env"
        )

    # Map speakers to voice IDs
    # Default voices: Adam (male) and Emily (female) from ElevenLabs
    voice_map = {
        "A": tts_config.get("host_a_voice_id", "CwhRBWXzGAHq8TQ4Fs17"),
        "B": tts_config.get("host_b_voice_id", "EXAVITQu4vr4xnSDxMaL"),
    }

    # Initialize ElevenLabs client
    from elevenlabs import ElevenLabs
    client = ElevenLabs(api_key=api_key)

    # Step 2: Generate audio for each segment
    logger.info(f"Generating TTS for {len(script_segments)} segments...")
    audio_chunks = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, segment in enumerate(script_segments):
            speaker = segment["speaker"]
            text = segment["text"]
            voice_id = voice_map.get(speaker, voice_map["A"])

            speaker_name = "Alex" if speaker == "A" else "Sam"
            preview = text[:60] + "..." if len(text) > 60 else text
            logger.info(f"  Segment {i+1}/{len(script_segments)} ({speaker_name}): {preview}")

            try:
                # Call ElevenLabs API — returns an audio generator
                audio_generator = client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=text,
                    model_id="eleven_multilingual_v2",  # Best quality model
                    output_format="mp3_44100_128",      # Good quality MP3
                )

                # Save the audio chunk to a temp file
                chunk_path = Path(tmp_dir) / f"chunk_{i:04d}.mp3"
                with open(chunk_path, "wb") as f:
                    for audio_bytes in audio_generator:
                        f.write(audio_bytes)

                audio_chunks.append(chunk_path)

            except Exception as e:
                logger.error(f"  TTS failed for segment {i+1}: {e}")
                if "quota_exceeded" in str(e) or "payment_required" in str(e):
                    logger.warning(
                        f"  Quota/payment issue — stitching {len(audio_chunks)} "
                        f"of {len(script_segments)} segments into a partial episode."
                    )
                    break  # Stitch what we have instead of crashing
                raise

        if not audio_chunks:
            raise RuntimeError("No audio segments were generated. Check your ElevenLabs API key and quota.")

        # Step 3: Stitch all chunks together
        logger.info("Stitching audio segments together...")

        # Create silence gap between speakers (300ms of quiet)
        silence = AudioSegment.silent(duration=300)

        # Load and combine all chunks
        combined = AudioSegment.empty()
        for i, chunk_path in enumerate(audio_chunks):
            chunk_audio = AudioSegment.from_mp3(str(chunk_path))
            if i > 0:
                # Add a brief silence between segments for natural pacing
                combined += silence
            combined += chunk_audio

        # Step 4: Add fade effects for a polished feel
        # Gentle fade-in at the start (500ms)
        combined = combined.fade_in(500)
        # Longer fade-out at the end (1000ms) for a smooth finish
        combined = combined.fade_out(1000)

        # Step 5: Export the final MP3
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        episodes_dir = data_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        output_path = episodes_dir / f"{timestamp}.mp3"

        combined.export(str(output_path), format="mp3", bitrate="128k")

        # Log the results
        duration_seconds = len(combined) / 1000
        duration_min = int(duration_seconds // 60)
        duration_sec = int(duration_seconds % 60)
        file_size_mb = output_path.stat().st_size / (1024 * 1024)

        logger.info(f"Audio saved: {output_path}")
        logger.info(f"Duration: {duration_min}m {duration_sec}s | Size: {file_size_mb:.1f}MB")

        return output_path


if __name__ == "__main__":
    """
    Run this directly to test TTS:
      python speak.py                    # Use today's saved script
      python speak.py --date 2026-03-14  # Use a specific day's script
    """
    import argparse

    parser = argparse.ArgumentParser(description="Generate podcast audio from a saved script")
    parser.add_argument("--date", help="Date of the script to use (YYYY-MM-DD)", default=None)
    args = parser.parse_args()

    logger = setup_logging()
    load_env()
    config = load_config()

    # Load a previously generated script
    from remix import load_saved_script
    script = load_saved_script(args.date)

    logger.info(f"Loaded script with {len(script)} segments")
    output_path = generate_audio(script, config, logger)
    print(f"\nAudio file created: {output_path}")
