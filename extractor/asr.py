"""Speech-to-text with faster-whisper, mlx-whisper or Gemini.

Backends are imported lazily, so you only need to install the one you actually use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Default model per backend.
# PhoWhisper is VinAI's Whisper fine-tuned for Vietnamese; the ct2 build below was converted
# by the community. To convert VinAI's original yourself:
#   ct2-transformers-converter --model vinai/PhoWhisper-large \
#       --output_dir phowhisper-large-ct2 --quantization float16
DEFAULT_MODELS = {
    "faster-whisper": "kiendt/PhoWhisper-large-ct2",
    "mlx-whisper": "mlx-community/whisper-large-v3-mlx",
}


def extract_audio(video: Path, out: Path) -> Path:
    """Extract 16 kHz mono audio, the format Whisper expects."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        check=True,
    )
    return out


def transcribe(audio: Path, backend: str, model: str | None, language: str = "vi") -> list[dict]:
    """Return [{start, end, text}] in seconds from the start of the file."""
    model = model or DEFAULT_MODELS.get(backend)
    if backend == "faster-whisper":
        return _faster_whisper(audio, model, language)
    if backend == "mlx-whisper":
        return _mlx_whisper(audio, model, language)
    raise ValueError(f"Invalid ASR backend: {backend}")


def _faster_whisper(audio: Path, model: str, language: str) -> list[dict]:
    from faster_whisper import WhisperModel  # type: ignore

    # CTranslate2 has no Metal backend, so on a Mac this runs on the CPU.
    try:
        m = WhisperModel(model, device="cuda", compute_type="float16")
        print("ASR: faster-whisper on GPU (float16)")
    except Exception as e:
        print(f"GPU not available ({e}); running on CPU int8, this will be slower.")
        m = WhisperModel(model, device="cpu", compute_type="int8")

    segments, info = m.transcribe(
        str(audio), language=language, beam_size=5,
        vad_filter=True, condition_on_previous_text=False,
    )
    print(f"  audio length: {info.duration:.0f}s")
    out = []
    for seg in segments:
        out.append({"start": seg.start, "end": seg.end, "text": seg.text})
        if len(out) % 100 == 0:
            print(f"  transcribed up to {seg.end:.0f}s")
    return out


def _mlx_whisper(audio: Path, model: str, language: str) -> list[dict]:
    import mlx_whisper  # type: ignore

    print("ASR: mlx-whisper (Metal, Apple Silicon)")
    result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model, language=language)
    return [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
        for seg in result.get("segments", [])
    ]


def split_audio(audio: Path, out_dir: Path, chunk_seconds: int, total_seconds: int) -> list[tuple[int, Path]]:
    """Cut audio into chunks to send to Gemini. Return [(start second, file)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, start in enumerate(range(0, total_seconds, chunk_seconds)):
        f = out_dir / f"chunk_{i:03d}.m4a"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(start), "-t", str(chunk_seconds), "-i", str(audio),
             "-vn", "-c:a", "aac", "-b:a", "64k", str(f)],
            check=True,
        )
        out.append((start, f))
    return out
