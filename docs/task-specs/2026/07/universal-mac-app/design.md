# Universal macOS App — Design

**Date:** 2026-07-21 · **Branch:** `feat/universal-mac-app` · **Status:** approved (chat)

## Problem

`make desktop` produces a host-arch-only DMG. Built on Apple Silicon, the DMG
is arm64-only: Intel Macs can't run it, and CI ships only
`unsigned-build-darwin-arm64`. Goal: **one universal DMG** that runs natively
on both arches, distributed through the existing single-artifact
channel feed (no per-arch feed split).

## Decision

**Universal Electron shell + dual embedded backends.** electron-builder's
`--universal` target lipo-merges the shell binaries; the python-build-standalone
(PBS) backend tree cannot be lipo-merged (thousands of files, no universal2
PBS, not all deps publish paired wheels), so the app ships both per-arch
backend trees and selects at launch via `process.arch`.

Rejected: true universal2 backend (fragile file-by-file merge, no tool
support); x86_64-only under Rosetta (penalizes the Apple-Silicon majority,
Rosetta being wound down); per-arch DMGs (requires per-arch feed split —
explicitly deferred).

## Bundle layout

```
KiroCrew.app/Contents/
├── MacOS/ + Frameworks/…                 ← fat binaries (arm64 + x86_64)
└── Resources/backend-dist/
    ├── kirocrew-backend-arm64/           ← full PBS bundle, arm64
    └── kirocrew-backend-x64/             ← full PBS bundle, x86_64
```

DMG name: `KiroCrew-<version>-universal.dmg` (~350–400MB). On macOS,
universal is the **default** for `make desktop`; `UNIVERSAL=0` opts out into
the host-arch-only build, whose unsuffixed `kirocrew-backend/` layout remains
valid (and is what Linux and `make backend-bin` always use).

## Components

1. **`packaging/build-desktop.sh` — universal by DEFAULT on macOS**
   (`UNIVERSAL` defaults to 1 on darwin, 0 elsewhere; `UNIVERSAL=0` opts a
   macOS build out for faster host-arch-only local iteration — final decision,
   superseding the earlier separate-target plan; macOS-only mode, fail fast
   elsewhere). Frontend built once. arm64 backend = today's steps →
   `backend-dist/kirocrew-backend-arm64/`. x86_64 backend = same steps with an
   x86_64 PBS interpreter (`uv python install cpython-3.12-macos-x86_64-none`;
   the x86_64 python binary executes under Rosetta) →
   `backend-dist/kirocrew-backend-x64/`. Preflight: Rosetta present
   (`arch -x86_64 /usr/bin/true`) else fail with the `softwareupdate` hint.
   Per-backend self-containment gate as today (the x64 gate also proves the
   bundle runs under Rosetta). Then `electron-builder --mac --universal`.
   Post-gates: `lipo -archs` on the app binary shows `x86_64 arm64`; `file` on
   each backend's `bin/python3.12` shows the matching arch; resolver-agreement
   gate extended to assert the arch-suffixed launcher resolves.
2. **`Makefile`** — `make desktop` IS the universal build on macOS (no
   separate target; `backend-bin` pins `UNIVERSAL=0` since the standalone
   backend is a local-machine artifact).
3. **`website/electron/find-bin.js`** — arch-suffixed candidates ranked above
   the existing ones: `backend-dist/kirocrew-backend-<arch>/bin/kirocrew` for
   `<arch>` = `process.arch` (`arm64`|`x64`), then the unsuffixed fallback.
   Arch injected as a parameter (pure function, both branches unit-testable).
4. **`website/electron/package.json`** — `build.mac.x64ArchFiles` covering
   `Resources/backend-dist/**` (single-arch Mach-O files inside a universal
   app are intentional); `extraResources` ships `backend-dist/` wholesale so
   single- and dual-backend layouts both package.
5. **Intel embeddings** — no official macOS x86_64 llama-cpp-python wheel
   exists (verified: PyPI + the CPU wheel index + GitHub releases are
   arm64-only for macOS). Build the 0.3.34 sdist from source for x86_64
   (pinned version; `CMAKE_OSX_ARCHITECTURES=x86_64`, Metal OFF, CPU-only),
   extract the same lib closure (`libllama` + `libggml*`) into
   `src/kiro_crew/_vendor/llama_cpp_libs/macos_x86_64/`, record provenance +
   sha256s in `_vendor/README.md`. Map `darwin`/`x86_64` →
   `"macos_x86_64"` in `embeddings.py:_platform_libs_dirname()`. Add the dir
   to `setup.cfg [options.package_data]` (and `MANIFEST.in` if patterned).
   Gate: import the vendored `llama_cpp` under Rosetta with an x86_64 python
   and `LLAMA_CPP_LIB_PATH` pointing at the new dir.
6. **CI** — `build-desktop.yml`: the `macos-14` matrix entry keeps running
   `make desktop` (now universal by default), artifact renamed
   `unsigned-build-darwin-universal` (GitHub arm64 macOS runners include
   Rosetta 2). `sign-and-notarize.yml`: the mac-zip glob
   `find release -name "*arm64*.zip"` becomes arch-agnostic (universal zips
   drop the arch token). Everything downstream (codesign both slices,
   notarytool, staple, spctl, feed write) is arch-indifferent. Feed schema
   untouched: `latest-mac.json` points at the universal zip; installed arm64
   apps update onto it seamlessly.
7. **Docs** — `docs/DESKTOP_APP.md`: rewrite the "no universal2" section to
   describe this mode and why true universal2 stays off the table;
   `_vendor/README.md` table row for `macos_x86_64/`.

## Testing

- `website/electron/test/find-bin.test.js`: arm64 → arm64 dir; x64 → x64 dir;
  unsuffixed fallback when suffixed dirs absent.
- `test/test_embeddings.py`: `darwin`/`x86_64` mapping.
- Build gates in-script (self-containment ×2, resolver-agreement, lipo, file).
- Manual: build on Apple Silicon; launch natively; run
  `arch -x86_64 …/kirocrew-backend-x64/bin/kirocrew --version`.

## Out of scope

Per-arch feed split; Windows/Linux packaging; signing infra changes beyond
the one glob; deleting the per-arch build mode.
