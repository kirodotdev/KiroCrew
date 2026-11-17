# testpaths-ok: standalone tests of this small repo, run as a script
from attach_names import safe_name, stored_name


def test_unsafe_characters_become_underscores():
    assert safe_name("my photo (1).png") == "my_photo_1_.png"


def test_a_leading_dot_is_dropped():
    assert safe_name(".hidden.png") == "hidden.png"


def test_an_empty_result_reads_image():
    assert safe_name("...") == "image"


def test_stored_name_prefixes_the_digest():
    assert stored_name("abc123", "shot.png") == "abc123-shot.png"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
