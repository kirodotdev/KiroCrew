"""PPTX Maker — a ``pdftoppm``-compatible PDF rasterizer, on every OS.

The presentation engine turns a deck into slide thumbnails by shelling out to
``pdftoppm`` (poppler)::

    pdftoppm -png -scale-to 1280 slides.pdf <dir>/page

and then globbing ``page-<N>.png`` back. On a stock machine poppler is absent, so
that step failed and the app reported *"poppler is not installed, so slide
thumbnails and PDF preview are unavailable"* with no way forward — poppler is a
system package, and installing one from a browser request is exactly what this
app refuses to do (see ``docs/system-specs/modules/pptx-maker.md``).

This module closes the gap without installing anything. ``pypdfium2`` — a
self-contained PDF renderer with prebuilt wheels for macOS/Linux/Windows on both
architectures — is ALREADY a dependency of the engine's own venv, so the
capability is on disk the moment the engine is provisioned. What was missing is
the ``pdftoppm``-shaped command the engine looks for. :mod:`.preview_tools`
writes a tiny launcher named ``pdftoppm`` into the app's managed bin dir that
runs this module inside that venv.

Why a CLI shim rather than patching the engine: the engine is a digest-pinned
third-party tree that is replaced wholesale on every version bump, so an edit
there would be reverted by the next update. The ``argv`` contract is the stable
seam.

Deliberately NOT a full poppler: only the flags the engine actually passes are
implemented, and anything else is refused loudly rather than silently ignored —
a shim that accepted ``-tiff`` and emitted PNG would corrupt a caller's output.
A real poppler takes precedence, so this is the fallback and never an override:
:func:`.engine.optional_dep_path` probes ``PATH`` before the managed directory, and
— the part that actually decides which binary RUNS, since the engine calls
``which()`` inside its own MCP server — :func:`.provision.mcp_tools_path` appends
the managed directory to that server's ``PATH`` rather than prepending it.

Runs as ``__main__`` inside the ENGINE's venv (which owns ``pypdfium2``), never
in the gateway process.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Exit codes mirror poppler's convention closely enough for the engine, which
#: only distinguishes zero from non-zero.
_EXIT_OK = 0
_EXIT_ERROR = 1

#: ``pdftoppm`` numbers its output pages from 1 and zero-pads nothing, so a
#: 10-page document yields ``page-1.png`` … ``page-10.png``. The engine's regex
#: (``page-(\d+)\.png``) depends on exactly that, so it is reproduced here rather
#: than "improved" with padding.
_OUTPUT_TEMPLATE = "{prefix}-{page}.png"


def _build_parser() -> argparse.ArgumentParser:
    """The subset of poppler's interface the engine relies on.

    ``add_help=False``: poppler's own ``pdftoppm`` has no ``--help`` in the
    argparse sense and this is a compatibility surface, not a new tool.
    """
    parser = argparse.ArgumentParser(prog="pdftoppm", add_help=False)
    parser.add_argument("-png", action="store_true", dest="png")
    parser.add_argument("-scale-to", type=int, dest="scale_to", default=0)
    parser.add_argument("-r", type=float, dest="resolution", default=0.0)
    parser.add_argument("-f", type=int, dest="first_page", default=0)
    parser.add_argument("-l", type=int, dest="last_page", default=0)
    parser.add_argument("pdf")
    # Optional so a bare `pdftoppm file.pdf` (poppler writes to stdout) is
    # rejected by us with a clear message instead of an argparse usage error.
    parser.add_argument("prefix", nargs="?", default="")
    return parser


def _render_scale(width: float, height: float, scale_to: int, resolution: float) -> float:
    """The pypdfium2 render scale matching poppler's sizing flags.

    ``-scale-to N`` fits the LONG edge to N pixels — poppler scales the larger
    dimension and preserves aspect ratio, which is why the engine gets 1280px
    thumbnails from portrait and landscape decks alike. ``-r DPI`` is relative to
    PDF user space, which is 72 units per inch.

    Falls back to poppler's own default of 150 DPI when neither flag is given.
    """
    if scale_to > 0:
        longest = max(width, height)
        return (scale_to / longest) if longest > 0 else 1.0
    if resolution > 0:
        return resolution / 72.0
    return 150.0 / 72.0


def main(argv: list[str] | None = None) -> int:
    """Rasterize a PDF to ``<prefix>-<page>.png``. Returns a process exit code."""
    parser = _build_parser()
    try:
        args, unknown = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        # argparse exits on a malformed argv; report it the way a CLI does.
        print("pdftoppm shim: could not parse arguments", file=sys.stderr)
        return _EXIT_ERROR

    # Refuse rather than silently mis-render. An unrecognised flag means the
    # caller wanted behaviour this shim does not have (a different raster format,
    # cropping, greyscale), and honouring only the flags we know would hand back
    # output that does not match the request.
    if unknown:
        print(
            f"pdftoppm shim: unsupported option(s) {' '.join(unknown)}; "
            "install poppler for the full pdftoppm interface",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    if not args.prefix:
        print(
            "pdftoppm shim: an output prefix is required (writing images to stdout "
            "is not supported)",
            file=sys.stderr,
        )
        return _EXIT_ERROR

    # Refuse a nonsensical size rather than render at a size nobody asked for.
    # argparse accepts `-scale-to -1280` as the integer -1280, and a negative value
    # fails the `> 0` tests below and falls through to the 150 DPI default — so the
    # caller silently gets differently-sized PNGs instead of an error.
    for flag, value in (("-scale-to", args.scale_to), ("-r", args.resolution)):
        if value < 0:
            print(f"pdftoppm shim: {flag} must be positive, got {value}", file=sys.stderr)
            return _EXIT_ERROR

    try:
        # Imported inside main() so a missing/broken pypdfium2 is a reported CLI
        # error rather than an import-time traceback, and so `--help`-style
        # arg errors above cost nothing.
        import pypdfium2 as pdfium
    except ImportError as exc:
        print(f"pdftoppm shim: pypdfium2 is unavailable ({exc})", file=sys.stderr)
        return _EXIT_ERROR

    source = Path(args.pdf)
    if not source.is_file():
        print(f"pdftoppm shim: no such file: {source}", file=sys.stderr)
        return _EXIT_ERROR

    prefix = Path(args.prefix)
    try:
        if prefix.parent and not prefix.parent.exists():
            prefix.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"pdftoppm shim: cannot create {prefix.parent}: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    try:
        document = pdfium.PdfDocument(str(source))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a CLI error
        print(f"pdftoppm shim: cannot open {source}: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    try:
        total = len(document)
        # Poppler's -f/-l are 1-based and INCLUSIVE; 0 means "unset".
        first = args.first_page if args.first_page > 0 else 1
        last = args.last_page if args.last_page > 0 else total
        first = max(1, first)
        last = min(total, last)
        if first > last:
            print(
                f"pdftoppm shim: page range {first}-{last} is empty for a "
                f"{total}-page document",
                file=sys.stderr,
            )
            return _EXIT_ERROR

        for number in range(first, last + 1):
            page = document[number - 1]
            scale = _render_scale(
                page.get_width(), page.get_height(), args.scale_to, args.resolution
            )
            image = page.render(scale=scale).to_pil()
            target = Path(_OUTPUT_TEMPLATE.format(prefix=str(prefix), page=number))
            image.save(target)
    except Exception as exc:  # noqa: BLE001 - a render failure must be an exit code
        print(f"pdftoppm shim: rendering failed: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - best-effort release
            pass

    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
