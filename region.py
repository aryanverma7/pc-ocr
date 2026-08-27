"""
Pure logic for the calibrated capture region - deliberately separated
from calibrate.py's GUI code and agent.py's actual screen-capture code,
since THIS logic can be genuinely tested (region validation, coordinate
normalization, cropping math against a real image), while a live GUI and
real screen capture can't be tested without an actual display and Windows
environment.
"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "agent_config.json"


def normalize_region(x1: int, y1: int, x2: int, y2: int) -> dict:
    """
    Converts two arbitrary drag points into a well-formed region dict -
    handles dragging in any direction (bottom-right to top-left is just as
    valid as top-left to bottom-right), and rejects a zero-size selection.
    """
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    width = right - left
    height = bottom - top

    if width <= 0 or height <= 0:
        raise ValueError("Selected region has no area - drag a real rectangle, not a single point")

    return {"left": left, "top": top, "width": width, "height": height}


def crop_to_region(full_image, region: dict):
    """
    Crops a full screenshot down to the calibrated region. Takes any
    PIL-Image-like object with a .crop() method - kept generic so this can
    be tested against a real, synthetically-generated PIL Image without
    needing an actual screen capture.
    """
    left = region["left"]
    top = region["top"]
    right = left + region["width"]
    bottom = top + region["height"]
    return full_image.crop((left, top, right, bottom))


def save_region(region: dict) -> None:
    config = load_config()
    config["region"] = region
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return default_config()
    return json.loads(CONFIG_PATH.read_text())


def default_config() -> dict:
    return {
        "backend_url": "https://hub.dualbladex.org/api/ocr/credit-report",
        "agent_secret": "",
        # Long enough to cover a whole Valorant buy phase rather than the
        # first few seconds of one. It was 6, which is shorter than the
        # shopping itself: the burst expired while the menu was still open,
        # so the final "min next round" - the only reading that is actually
        # true - was never captured. Nothing depends on this being tight,
        # because the run of unreadable frames that follows a close ends
        # the burst about a second and a half later however the menu was
        # closed - Esc, a second B press, or the round simply starting.
        "burst_duration_seconds": 30,
        # How far apart two B presses have to be before the second one is
        # treated as a new round rather than a second look at the same buy
        # phase. See burst_timer.NEW_ROUND_GAP_SECONDS, which this
        # overrides and whose comment explains the choice; the Mac Mini
        # holds the same number as credit_ocr._READING_MAX_AGE_SECONDS.
        "new_round_gap_seconds": 20,
        "region": None,
    }


def is_calibrated(config: dict) -> bool:
    return config.get("region") is not None
