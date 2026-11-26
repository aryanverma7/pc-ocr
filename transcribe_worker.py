"""
The child process that does one transcript, so the parent can kill it (and free
the GPU) the moment a game starts.

    python transcribe_worker.py <media_path> <out_json> <model_dir>
"""
import json
import os
import sys
import sysconfig
from pathlib import Path

MODEL_NAME = "hinglish-apex-ct2"


def _cuda_dlls() -> None:
    """The nvidia-cublas/cudnn wheels keep their DLLs where Windows does not look."""
    if sys.platform != "win32":
        return
    for bin_dir in (Path(sysconfig.get_paths()["purelib"]) / "nvidia").glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def transcribe(media, model_dir) -> list:
    _cuda_dlls()
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_dir), device="cuda", compute_type="float16")
    # Apex emits romanized Hinglish under the English token; PyAV decodes the audio, no ffmpeg.
    segments, _ = model.transcribe(str(media), language="en", vad_filter=True, beam_size=5)
    lines = []
    for s in segments:
        text = s.text.strip()[:2000]
        if text:
            start = round(max(s.start, 0.0), 2)
            lines.append({"start": start, "end": round(max(s.end, start), 2), "text": text})
    return lines


def main(argv) -> int:
    media, out, model_dir = argv
    data = {"segments": transcribe(media, model_dir), "model": MODEL_NAME}
    tmp = Path(out).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
