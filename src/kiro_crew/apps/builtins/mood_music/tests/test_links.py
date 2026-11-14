"""Which song links Mood Music accepts: an 11-char YouTube id, an exact bilibili host."""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.mood_music.backend import links

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("value", [ID, "a-b_c-d_e-f"])
def test_a_video_id_is_eleven_safe_characters(value: str) -> None:
    assert links.valid_video_id(value) == value


@pytest.mark.parametrize(
    "value",
    [ID + "Q", ID[:10], "dQw4w9WgXc!", "dQw4w9WgXc/", "", None, 12345678901, f"{ID}\n"],
)
def test_anything_else_is_not_a_video_id(value: object) -> None:
    assert links.valid_video_id(value) is None


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={ID}",
        f"https://youtube.com/watch?v={ID}&t=30",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}",
    ],
)
def test_a_watch_or_share_link_gives_its_id(url: str) -> None:
    assert links.youtube_video_id(url) == ID


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.youtube.com/watch?v={ID}",  # not https
        f"https://www.youtube.com/watch?v={ID}Q",  # 12-char id
        f"https://www.youtube.com/watch?v={ID}&v={ID}",  # two ids
        f"https://www.youtube.com/embed/{ID}",  # not a watch link
        f"https://www.youtube.com.evil.com/watch?v={ID}",
        f"https://user@www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com:8443/watch?v={ID}",
        f"https://www.youtube.com\\@evil.com/watch?v={ID}",
        f"https://youtu.be/{ID}/x",
        "not a url",
        None,
    ],
)
def test_other_youtube_shapes_give_no_id(url: object) -> None:
    assert links.youtube_video_id(url) is None


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.bilibili.com/video/BV1xx411c7mD",
        ),
        (
            "https://m.bilibili.com/video/BV1xx411c7mD?p=2#x",
            "https://m.bilibili.com/video/BV1xx411c7mD?p=2",
        ),
        ("https://B23.TV/abc", "https://b23.tv/abc"),
    ],
)
def test_a_bilibili_link_is_rebuilt_from_its_parts(url: str, kept: str) -> None:
    assert links.bilibili_url(url) == kept


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bilibili.com/video/BV1xx411c7mD",
        "https://bilibili.com.evil.com/video/BV1",
        "https://www.bilibili.com.evil.com/video/BV1",
        "https://evil.com/?www.bilibili.com",
        "https://bilibili.com/video/BV1",  # bare domain is not on the list
        "https://space.bilibili.com/1",
        "https://user:pw@www.bilibili.com/video/BV1",
        "https://www.bilibili.com:444/video/BV1",
        "https://www.bilibili.com./video/BV1",
        "javascript:alert(1)",
        "https://www.bilibili.com/video/BV1 x",
        "",
        None,
    ],
)
def test_any_other_host_or_scheme_is_refused(url: object) -> None:
    assert links.bilibili_url(url) is None
