"""The Knowledge Library's Outlook .msg reader (``FileReader._read_msg``).

A .msg is an OLE compound file (MS-CFB). The fixtures are built byte by byte:
a 512-byte header, one FAT sector, one MiniFAT sector, the directory, then the
mini stream that holds every stream under 4096 bytes, which is where Outlook
keeps a message's strings.
"""

from __future__ import annotations

import struct
from pathlib import Path

from kiro_crew.knowledge.readers import FileReader

_FREE, _END, _FATSECT, _NONE = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _header(
    *, fat_sectors: int, first_dir: int, first_difat: int = _END, difat_sectors: int = 0
) -> bytes:
    head = bytearray(512)
    head[:8] = _MAGIC
    # minor version, major version 3, byte order, sector shift 9, mini sector shift 6
    struct.pack_into("<HHHHH", head, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into(
        "<IIIIIIIII",
        head,
        0x28,
        0,
        fat_sectors,
        first_dir,
        0,
        4096,
        1,
        1,
        first_difat,
        difat_sectors,
    )
    struct.pack_into("<109I", head, 0x4C, 0, *([_FREE] * 108))
    return bytes(head)


def _entry(
    name: str,
    kind: int,
    *,
    child: int = _NONE,
    right: int = _NONE,
    start: int = _END,
    size: int = 0,
) -> bytes:
    encoded = (name + "\0").encode("utf-16-le")
    return struct.pack(
        "<64sHBBIII16sIQQIQ",
        encoded,
        len(encoded),
        kind,
        1,
        _NONE,
        right,
        child,
        b"",
        0,
        0,
        0,
        start,
        size,
    )


def _cfb(
    entries: list[tuple[str, int, int]],
    mini: bytes,
    minifat: list[int],
    *,
    root_size: int = 0,
) -> bytes:
    """A compound file whose root holds *entries*, ``(name, start, size)`` streams in *mini*.

    A *root_size* declares the mini stream that long and makes its sector loop to itself.
    """
    directory = [
        _entry(name, 2, right=i + 1 if i < len(entries) else _NONE, start=start, size=size)
        for i, (name, start, size) in enumerate(entries, start=1)
    ]
    dir_sectors = -(-(len(directory) + 1) // 4)
    root_start = 2 + dir_sectors
    root_sectors = -(-len(mini) // 512)
    fat = [_FATSECT, _END]
    fat += [2 + k + 1 for k in range(dir_sectors - 1)] + [_END]
    fat += [root_start + k + 1 for k in range(root_sectors - 1)] + [
        root_start if root_size else _END
    ]
    root = _entry("Root Entry", 5, child=1, start=root_start, size=root_size or len(mini))
    return b"".join(
        [
            _header(fat_sectors=1, first_dir=2),
            struct.pack("<128I", *fat, *([_FREE] * (128 - len(fat)))),
            struct.pack("<128I", *minifat, *([_FREE] * (128 - len(minifat)))),
            b"".join([root, *directory]).ljust(dir_sectors * 512, b"\0"),
            mini.ljust(root_sectors * 512, b"\0"),
        ]
    )


def _msg(streams: dict[str, bytes], *, empty_start: int = _END) -> bytes:
    """A well-formed compound file holding *streams*, each under 4096 bytes."""
    mini = bytearray()
    minifat: list[int] = []
    entries = []
    for name, data in streams.items():
        if not data:
            entries.append((name, empty_start, 0))
            continue
        first = len(mini) // 64
        count = -(-len(data) // 64)
        minifat += [first + k + 1 for k in range(count - 1)] + [_END]
        mini += data.ljust(count * 64, b"\0")
        entries.append((name, first, len(data)))
    return _cfb(entries, bytes(mini), minifat)


def _unicode(text: str) -> bytes:
    return text.encode("utf-16-le")


def _submit_time() -> bytes:
    # The instant the test expects, as a FILETIME: 100 ns ticks since 1601-01-01.
    ticks = (1_794_346_689 + 11_644_473_600) * 10_000_000
    return bytes(32) + struct.pack("<IIQ", 0x00390040, 0, ticks)


def test_a_msg_reads_as_its_header_lines_and_body(tmp_path: Path) -> None:
    path = tmp_path / "escalation.msg"
    path.write_bytes(
        _msg(
            {
                "__substg1.0_0037001F": _unicode("Vendor escalation"),
                "__substg1.0_0C1A001F": _unicode("Ana Ops"),
                "__substg1.0_5D01001F": _unicode("ana@example.com"),
                "__substg1.0_0E04001F": _unicode("Support Team"),
                # Empty, with a start sector other than ENDOFCHAIN: it adds nothing and
                # must not cost the rest of the message.
                "__substg1.0_0E03001F": b"",
                "__substg1.0_1000001F": _unicode("Line one\r\nLine two"),
                "__properties_version1.0": _submit_time(),
            },
            empty_start=0,
        )
    )

    text, meta = FileReader().read(str(path))

    assert meta["format"] == "msg", text
    assert text == (
        "Subject: Vendor escalation\n"
        "From: Ana Ops <ana@example.com>\n"
        "Date: 2026-11-10 21:38:09 UTC\n"
        "To: Support Team\n"
        "\n"
        "Line one\nLine two"
    )


def test_a_header_declaring_more_fat_than_the_file_is_refused_unparsed(tmp_path: Path) -> None:
    # 2,048 bytes whose header claims 109 + 127 * 2 FAT sectors, listed by a DIFAT
    # sector that points back at itself: olefile would loop over every one.
    looping_difat = struct.pack("<128I", *([0] * 127), 1)
    data = _header(fat_sectors=109 + 127 * 2, first_dir=2, first_difat=1, difat_sectors=2)
    path = tmp_path / "crafted.msg"
    path.write_bytes((data + looping_difat).ljust(2048, b"\0"))

    _text, meta = FileReader().read(str(path))

    assert meta == {**meta, "format": "error", "error": "malformed .msg header"}


def test_streams_that_share_a_sector_still_read(tmp_path: Path) -> None:
    # olefile's duplicate-stream scan, quadratic in directory entries on a crafted
    # file, is skipped: sharing alone does not fail a message.
    subject = _unicode("Shared")
    path = tmp_path / "shared.msg"
    path.write_bytes(
        _cfb(
            [("__substg1.0_0037001F", 0, len(subject)), ("__substg1.0_1000001F", 0, len(subject))],
            subject.ljust(64, b"\0"),
            [_END],
        )
    )

    text, meta = FileReader().read(str(path))

    assert (text, meta["format"]) == ("Subject: Shared\n\nShared", "msg")


def test_looping_streams_larger_than_the_file_are_refused(tmp_path: Path) -> None:
    # Three 4,000-byte streams on one mini sector whose MiniFAT entry points at
    # itself: each fits the FAT, together they are larger than the whole file.
    names = ("__substg1.0_0037001F", "__substg1.0_0E04001F", "__substg1.0_1000001F")
    data = _cfb([(name, 0, 4000) for name in names], _unicode("loop").ljust(4032, b"\0"), [0] * 63)
    path = tmp_path / "looping.msg"
    path.write_bytes(data)
    assert len(data) < 3 * 4000

    _text, meta = FileReader().read(str(path))

    assert meta["format"] == "error"
    assert "larger than the file" in meta["error"]


def test_a_mini_stream_declared_larger_than_the_file_is_refused(tmp_path: Path) -> None:
    # The root declares an 8 MB mini stream on one sector that loops to itself;
    # olefile reads up to the declared size unless it stops at the FAT's length.
    subject = _unicode("Hi")
    path = tmp_path / "inflated.msg"
    path.write_bytes(
        _cfb(
            [("__substg1.0_0037001F", 0, len(subject))],
            subject.ljust(64, b"\0"),
            [_END],
            root_size=8_000_000,
        )
    )

    _text, meta = FileReader().read(str(path))

    assert meta["format"] == "error"
    assert "MiniStream is larger than MiniFAT" in meta["error"]


def test_a_plain_text_msg_reads_as_text(tmp_path: Path) -> None:
    # .msg also names text files, such as ROS message definitions.
    path = tmp_path / "Pose.msg"
    path.write_bytes(b"# A pose\nfloat64 x\nfloat64 y\n")

    text, meta = FileReader().read(str(path))

    assert (text, meta["format"]) == ("# A pose\nfloat64 x\nfloat64 y\n", "msg")


def test_an_8_bit_message_reads_in_its_code_page(tmp_path: Path) -> None:
    subject = "Отчёт об инциденте"
    codepage = struct.pack("<IIQ", 0x3FFD0003, 0, 1251)  # PR_MESSAGE_CODEPAGE
    path = tmp_path / "ansi.msg"
    path.write_bytes(
        _msg(
            {
                "__substg1.0_0037001E": subject.encode("cp1251"),
                "__properties_version1.0": bytes(32) + codepage,
            }
        )
    )

    text, meta = FileReader().read(str(path))

    assert (text, meta["format"]) == (f"Subject: {subject}", "msg")
