"""
One-time: convert Oriserve/Whisper-Hindi2Hinglish-Apex to CTranslate2 float16
for faster-whisper. Needs torch, so run it anywhere with uv (WSL is fine) and
copy models/hinglish-apex-ct2 into the agent folder:

    uv run --no-project --with "transformers[torch]" --with ctranslate2 python convert_model.py
"""
from pathlib import Path

from ctranslate2.converters import TransformersConverter

OUT = Path(__file__).parent / "models" / "hinglish-apex-ct2"

if __name__ == "__main__":
    TransformersConverter("Oriserve/Whisper-Hindi2Hinglish-Apex",
                          copy_files=["tokenizer.json", "preprocessor_config.json"]
                          ).convert(str(OUT), quantization="float16")
    print(f"Converted to {OUT}")
