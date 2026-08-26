import json
import pytest
from PIL import Image

import region


class TestNormalizeRegion:
    def test_normalizes_a_normal_top_left_to_bottom_right_drag(self):
        result = region.normalize_region(100, 200, 300, 250)
        assert result == {"left": 100, "top": 200, "width": 200, "height": 50}

    def test_normalizes_a_reversed_bottom_right_to_top_left_drag(self):
        """
        Dragging is directionless in practice - starting the drag from the
        opposite corner must give the exact same result as the "normal"
        direction, not a broken or negative-size region.
        """
        result = region.normalize_region(300, 250, 100, 200)
        assert result == {"left": 100, "top": 200, "width": 200, "height": 50}

    def test_normalizes_a_diagonal_drag_from_bottom_left_to_top_right(self):
        result = region.normalize_region(100, 250, 300, 200)
        assert result == {"left": 100, "top": 200, "width": 200, "height": 50}

    def test_rejects_a_zero_width_selection(self):
        with pytest.raises(ValueError):
            region.normalize_region(100, 100, 100, 200)

    def test_rejects_a_zero_height_selection(self):
        with pytest.raises(ValueError):
            region.normalize_region(100, 100, 200, 100)

    def test_rejects_a_single_point_with_no_area_at_all(self):
        with pytest.raises(ValueError):
            region.normalize_region(100, 100, 100, 100)


class TestCropToRegion:
    def test_crops_a_real_image_to_the_exact_calibrated_region(self):
        # A real 200x200 image, split into four distinct 100x100 colored
        # quadrants - lets the test confirm the crop grabs EXACTLY the
        # right pixels, not just the right dimensions.
        full = Image.new("RGB", (200, 200))
        full.paste((255, 0, 0), (0, 0, 100, 100))      # top-left: red
        full.paste((0, 255, 0), (100, 0, 200, 100))    # top-right: green
        full.paste((0, 0, 255), (0, 100, 100, 200))    # bottom-left: blue
        full.paste((255, 255, 0), (100, 100, 200, 200))  # bottom-right: yellow

        cropped = region.crop_to_region(full, {"left": 100, "top": 100, "width": 100, "height": 100})

        assert cropped.size == (100, 100)
        # Sample the center of the crop - should be solidly yellow (the
        # bottom-right quadrant), confirming the crop grabbed the correct
        # region, not just a correctly-SIZED but wrong-LOCATION crop.
        assert cropped.getpixel((50, 50)) == (255, 255, 0)

    def test_crop_dimensions_match_the_region_exactly(self):
        full = Image.new("RGB", (1920, 1080))
        cropped = region.crop_to_region(full, {"left": 50, "top": 60, "width": 120, "height": 40})
        assert cropped.size == (120, 40)


class TestConfig:
    def test_default_config_is_not_calibrated(self):
        config = region.default_config()
        assert region.is_calibrated(config) is False

    def test_is_calibrated_true_once_a_region_is_present(self):
        config = region.default_config()
        config["region"] = {"left": 0, "top": 0, "width": 100, "height": 30}
        assert region.is_calibrated(config) is True

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        fake_config_path = tmp_path / "agent_config.json"
        monkeypatch.setattr(region, "CONFIG_PATH", fake_config_path)

        new_region = {"left": 10, "top": 20, "width": 100, "height": 30}
        region.save_region(new_region)

        loaded = region.load_config()
        assert loaded["region"] == new_region
        # Confirms the rest of the default config survived the save too -
        # save_region() must not silently wipe other settings like the
        # backend URL and secret when it writes the region.
        assert loaded["backend_url"] == region.default_config()["backend_url"]

    def test_loading_with_no_config_file_yet_returns_sensible_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(region, "CONFIG_PATH", tmp_path / "does_not_exist.json")
        config = region.load_config()
        assert region.is_calibrated(config) is False
        assert config["burst_duration_seconds"] > 0
