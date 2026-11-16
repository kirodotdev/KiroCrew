"""Hidden check for S1: a long image name keeps its extension when it is cut."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / "lib"))
from hidden import main  # noqa: E402

UPLOAD = "0" * 32 + "_Screenshot_2026-11-06_at_09.18.00.png"
# The required limits, fixed here: the submission's own constants are not trusted.
MAX_NAME_CHARS = 64
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")


def test_a_long_image_name_keeps_its_extension():
    from attach_names import safe_name

    out = safe_name(UPLOAD)
    assert out.endswith(".png"), out
    assert len(out) <= MAX_NAME_CHARS, out
    assert out.startswith("0" * 32 + "_Screenshot"), out


def test_every_image_extension_survives_and_case_is_kept():
    from attach_names import safe_name

    for ext in list(IMAGE_EXTENSIONS) + [".PNG", ".JpG"]:
        out = safe_name("x" * 100 + ext)
        assert out.endswith(ext) and len(out) <= MAX_NAME_CHARS, (ext, out)


def test_names_at_or_under_the_cap_are_unchanged():
    from attach_names import safe_name

    exact = "a" * (MAX_NAME_CHARS - 4) + ".png"
    assert safe_name(exact) == exact
    assert safe_name("shot.png") == "shot.png"
    assert safe_name("my photo (1).png") == "my_photo_1_.png"


def test_a_long_non_image_name_is_cut_as_before():
    from attach_names import safe_name

    name = "n" * 100 + ".txt"
    assert safe_name(name) == name[:MAX_NAME_CHARS]


def test_a_stem_of_only_dots_still_names_an_image():
    from attach_names import safe_name

    out = safe_name("." * 100 + "x.png")
    assert out.endswith(".png") and out != ".png", out


def test_the_visible_tests_still_pass():
    import test_attach_names as visible

    for name in dir(visible):
        if name.startswith("test_"):
            getattr(visible, name)()


if __name__ == "__main__":
    main(globals())
