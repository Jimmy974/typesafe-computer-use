from dataclasses import replace

import pytest
from PIL import Image

from typesafe_computer_use import perception
from typesafe_computer_use.perception import (
    OcrCache,
    boxes_intersect,
    changed_tiles,
    merge_reocr,
    ocr_crop,
    ocr_lines,
    ocr_region,
    reocr_rect,
    thumbnail,
    tiles_in,
)


def screen_with(window, width=2000, height=1200, scale=2.0, app="Google Chrome", image=None):
    return perception.Screen(
        image=image if image is not None else Image.new("RGB", (width, height)),
        scale=scale,
        app=app,
        field=None,
        url=None,
        window=window,
    )


# ------------------------------------------------------------------ the region


def test_region_is_the_whole_capture_without_a_window():
    assert ocr_region(screen_with(None)) == (0.0, 0.0, 2000.0, 1200.0)


def test_region_covers_the_window_with_a_margin_and_the_menu_bar():
    # window 100..500 pt vertically, scale 2, so 200..1000 px, plus an 8 pt margin below it
    region = ocr_region(screen_with((50.0, 100.0, 600.0, 400.0)))
    assert region == (0.0, 0.0, 2000.0, (100.0 + 400.0 + 8.0) * 2)


def test_region_reaches_the_menu_bar_even_for_a_window_low_on_the_display():
    region = ocr_region(screen_with((50.0, 400.0, 600.0, 100.0)))
    assert region[1] == 0.0  # the menu bar strip is always read


def test_region_is_at_least_the_menu_bar_strip_for_a_window_above_it():
    region = ocr_region(screen_with((50.0, 0.0, 600.0, 10.0)))
    assert region[3] == perception.MENU_BAR_PT * 2


def test_region_clamps_a_window_larger_than_the_display():
    assert ocr_region(screen_with((-100.0, -50.0, 4000.0, 3000.0))) == (0.0, 0.0, 2000.0, 1200.0)


def test_region_falls_back_to_the_whole_capture_for_a_degenerate_window():
    assert ocr_region(screen_with((0.0, 0.0, 0.0, 0.0), scale=0.0)) == (0.0, 0.0, 2000.0, 1200.0)


# ------------------------------------------------------------------ tiles and change detection


def test_tiles_cover_the_region_aligned_to_its_origin():
    tiles = tiles_in((100.0, 100.0, 700.0, 400.0), tile=256.0)
    assert len(tiles) == 3 * 2
    assert tiles[0] == (100.0, 100.0, 356.0, 356.0)
    assert tiles[-1] == (612.0, 356.0, 700.0, 400.0)  # the last row and column are short


def painted(size=(2048, 1024), boxes=()):
    image = Image.new("RGB", size, (30, 30, 30))
    for box in boxes:
        image.paste((255, 255, 255), box)
    return image


def test_no_tile_changes_between_identical_captures():
    thumb = thumbnail(painted((1024, 512)))
    tiles = tiles_in((0.0, 0.0, 1024.0, 512.0))
    assert changed_tiles(thumb, thumb, tiles) == []


def test_only_the_painted_tile_changes():
    before, after = painted((1024, 512)), painted((1024, 512), boxes=[(300, 300, 400, 400)])
    tiles = tiles_in((0.0, 0.0, 1024.0, 512.0))
    changed = changed_tiles(thumbnail(after), thumbnail(before), tiles)
    assert changed == [(256.0, 256.0, 512.0, 512.0)]


def test_a_whole_new_screen_changes_every_tile():
    before, after = painted((1024, 512)), painted((1024, 512), boxes=[(0, 0, 1024, 512)])
    tiles = tiles_in((0.0, 0.0, 1024.0, 512.0))
    assert len(changed_tiles(thumbnail(after), thumbnail(before), tiles)) == len(tiles)


def test_faint_noise_does_not_count_as_a_change():
    before = painted((1024, 512))
    after = before.point(lambda v: min(255, v + 3))
    tiles = tiles_in((0.0, 0.0, 1024.0, 512.0))
    assert changed_tiles(thumbnail(after), thumbnail(before), tiles) == []


def test_reocr_rect_pads_by_one_tile_and_clamps_to_the_region():
    region = (0.0, 0.0, 1024.0, 512.0)
    assert reocr_rect([(256.0, 256.0, 512.0, 512.0)], region) == (0.0, 0.0, 768.0, 512.0)
    assert reocr_rect([], region) is None


# ------------------------------------------------------------------ lines


def line(text, x1, y1, x2, y2, conf=1.0):
    return (text, conf, (float(x1), float(y1), float(x2), float(y2)))


def test_boxes_intersect_only_on_real_overlap():
    assert boxes_intersect((0, 0, 10, 10), (5, 5, 20, 20))
    assert not boxes_intersect((0, 0, 10, 10), (10, 0, 20, 10))


def test_reocr_replaces_every_line_the_rectangle_touches():
    """A line that only straddles the edge goes too: the fresh read covers it whole."""
    previous = [line("stale", 100, 100, 200, 130), line("kept", 900, 900, 1000, 930), line("straddles", 90, 40, 110, 70)]
    fresh = [line("fresh", 100, 100, 260, 130)]
    merged = merge_reocr(previous, fresh, (50.0, 50.0, 400.0, 400.0))
    assert [t for t, _, _ in merged] == ["kept", "fresh"]


def test_reocr_keeps_a_line_that_only_touches_the_rectangle_edge():
    previous = [line("outside", 10.0, 10.0, 50.0, 40.0)]
    assert merge_reocr(previous, [], (50.0, 10.0, 400.0, 400.0)) == previous


def test_ocr_crop_offsets_boxes_back_into_full_capture_coordinates(monkeypatch):
    class FakeOCR:
        def __init__(self, image, recognition_level="accurate"):
            self.image = image

        def recognize(self, px=True):
            assert self.image.size == (200, 100)  # the crop, not the capture
            return [("hello", 0.9, (10.0, 20.0, 60.0, 50.0))]

    monkeypatch.setattr(perception.ocrmac, "OCR", FakeOCR)
    ((text, conf, box),) = ocr_crop(Image.new("RGB", (1000, 800)), (300.0, 400.0, 500.0, 500.0))
    assert (text, conf) == ("hello", 0.9)
    assert box == (310.0, 420.0, 360.0, 450.0)


# ------------------------------------------------------------------ the reuse decision


@pytest.fixture
def reads(monkeypatch):
    """Record every rectangle handed to Vision, and answer with one line naming it."""
    seen = []

    def fake_crop(image, rect):
        seen.append(tuple(round(v) for v in rect))
        return [line(f"read {len(seen)}", rect[0], rect[1], rect[0] + 10, rect[1] + 10)]

    monkeypatch.setattr(perception, "ocr_crop", fake_crop)
    return seen


def capture_of(boxes=(), app="Google Chrome", window=(0.0, 0.0, 1024.0, 512.0)):
    """A 2048x1024 capture whose window covers it, so the region is the whole image."""
    return screen_with(window, image=painted(boxes=boxes), app=app, scale=2.0)


def test_first_capture_reads_the_whole_region(reads):
    cache = OcrCache()
    lines, pct = ocr_lines(capture_of(), cache)
    assert reads == [(0, 0, 2048, 1024)] and pct == 100.0
    assert [t for t, _, _ in lines] == ["read 1"]


def test_an_unchanged_capture_is_not_read_again(reads):
    cache = OcrCache()
    ocr_lines(capture_of(), cache)
    lines, pct = ocr_lines(capture_of(), cache)
    assert len(reads) == 1 and pct == 0.0
    assert [t for t, _, _ in lines] == ["read 1"]


def test_a_small_change_reads_only_the_rectangle_around_it(reads):
    cache = OcrCache()
    ocr_lines(capture_of(), cache)
    lines, pct = ocr_lines(capture_of(boxes=[(300, 300, 400, 400)]), cache)
    assert reads[1] == (0, 0, 768, 768) and pct < 50.0
    assert [t for t, _, _ in lines] == ["read 2"]  # the first read's line sat inside the rectangle


def test_a_change_over_the_threshold_reads_the_whole_region(reads):
    cache = OcrCache()
    ocr_lines(capture_of(), cache)
    _, pct = ocr_lines(capture_of(boxes=[(0, 0, 2048, 1024)]), cache)
    assert reads[1] == (0, 0, 2048, 1024) and pct == 100.0


def test_a_different_app_is_never_reused(reads):
    cache = OcrCache()
    first = capture_of()
    ocr_lines(first, cache)
    ocr_lines(replace(first, app="Slack"), cache)
    assert reads == [(0, 0, 2048, 1024), (0, 0, 2048, 1024)]


def test_a_moved_window_is_never_reused(reads):
    cache = OcrCache()
    first = capture_of()
    ocr_lines(first, cache)
    ocr_lines(replace(first, window=(20.0, 0.0, 1024.0, 512.0)), cache)
    assert len(reads) == 2 and reads[1] == (0, 0, 2048, 1024)


def test_the_rectangle_grows_past_a_line_it_would_have_cut_in_half(reads, monkeypatch):
    cache = OcrCache()
    ocr_lines(capture_of(), cache)
    cache.lines = [line("a headline running off to the right", 700.0, 300.0, 1400.0, 340.0)]
    ocr_lines(capture_of(boxes=[(300, 300, 400, 400)]), cache)
    assert reads[1] == (0, 0, 1400, 768)  # widened to take the whole headline


def test_a_rectangle_that_grows_past_the_threshold_reads_the_whole_region(reads):
    cache = OcrCache()
    ocr_lines(capture_of(), cache)
    cache.lines = [line("edge to edge", 0.0, 300.0, 2048.0, 900.0)]
    _, pct = ocr_lines(capture_of(boxes=[(300, 300, 400, 400)]), cache)
    assert reads[1] == (0, 0, 2048, 1024) and pct == 100.0


def test_no_cache_always_reads_the_region(reads):
    ocr_lines(capture_of(), None)
    ocr_lines(capture_of(), None)
    assert reads == [(0, 0, 2048, 1024), (0, 0, 2048, 1024)]
