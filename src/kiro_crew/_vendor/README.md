# Vendored third-party code

## llama-cpp-python 0.3.34 (MIT)

`llama_cpp/` is the Python package from [abetlen/llama-cpp-python](https://github.com/abetlen/llama-cpp-python)
v0.3.34, vendored so the in-process embedding runtime needs no runtime pip
install, no `--extra-index-url`, and no external Ollama server. License:
`llama_cpp/LICENSE.md` (MIT, includes bundled llama.cpp/ggml — also MIT).

What was changed relative to the upstream wheel:

- `llama_cpp/lib/` (bundled native libs) removed — per-platform libraries live
  in `llama_cpp_libs/<platform>/` instead and are selected at runtime via the
  `LLAMA_CPP_LIB_PATH` env var (upstream-supported override, see
  `llama_cpp/llama_cpp.py`).
- `llama_cpp/server/` removed (FastAPI server — unused, heavy deps).
- `llama_cpp.llama_cache`'s module-level `import diskcache` is satisfied by a
  `sys.modules` stub installed in `kiro_crew.embeddings._install_diskcache_stub`
  (KiroCrew never uses disk-backed LLM state caching; a real installed
  diskcache, if present, wins).

`llama_cpp_libs/` holds the minimal verified shared-library closure per
platform, extracted from the official prebuilt CPU wheels
(https://abetlen.github.io/llama-cpp-python/whl/cpu) — except `macos_x86_64/`,
which upstream does not publish and is built from the pinned PyPI sdist (see
below):

| Dir | Source wheel |
|-----|--------------|
| `linux_x86_64/`  | `llama_cpp_python-0.3.34-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` (sha256 f34c26f51ec4fd4e0355c5384b7056f877bf7a38c9d7897a46c78118ca900366) |
| `linux_aarch64/` | `llama_cpp_python-0.3.34-py3-none-manylinux2014_aarch64.manylinux_2_17_aarch64.whl` (sha256 725d8a324032b3f1143c20eee62e47415476eb85127e8a134e3a431d666d21d1) |
| `macos_arm64/`   | `llama_cpp_python-0.3.34-py3-none-macosx_11_0_arm64.whl` (sha256 d42e069db63c11494f429589fb0b7b5d3862d72d4ad5e8ef311e0ece7865b33d) |
| `macos_x86_64/`  | built from sdist `llama_cpp_python-0.3.34.tar.gz` (sha256 d849d286d808284f1d3ec1bd6875572430d29d1f9574a010232caa4e9cef0e35) — see below |
| `win_amd64/`     | `llama_cpp_python-0.3.34-py3-none-win_amd64.whl` (sha256 6526fff614e5ef7e439e6369e076a78073e45e1d791dbe1d5e5d42661f46ca1a) |

Linux libs are manylinux2014 (glibc ≥ 2.17) — they run on both AL2 (2.26) and
AL2023 (2.34). The macOS dylibs embed the Metal shader (no separate
`.metallib`); the x86_64 build compiles the Metal backend in but macOS
disables Metal under Rosetta/Intel at runtime, so ggml falls back to
CPU+Accelerate. Windows DLLs are found via `os.add_dll_directory`.

Linux carries no BLAS backend, and that is not a gap to fill: upstream ships
none in its Linux CPU wheels (`libggml-blas` exists on macOS only because it
links the system Accelerate framework), and the Linux `libggml-cpu` carries the
optimized GEMM/repack kernels instead.

### Every file here must reach the installed package

These libs are loaded by ctypes at runtime, so a single absent file makes the
whole runtime unusable and memory silently falls back to keyword search behind
one WARNING — nothing fails loudly. Three packaging lanes select these files by
three different mechanisms, and each can drop them alone:

| Lane | Mechanism | Gotcha |
|---|---|---|
| sdist | `MANIFEST.in` | `global-exclude *.so` strips exactly `libllama.so` (other Linux libs end `.so.0`; macOS/Windows use `.dylib`/`.dll`). The re-include MUST stay after every exclude — later rules win. `python -m build` builds the wheel FROM the sdist, so a loss here reaches every pip install |
| wheel | `setup.cfg [options.package_data]` | explicit per-platform globs, because setuptools' `**` recursion has varied across versions |
| desktop | `packaging/kirocrew-backend.spec` | walks the tree directly and never reads `MANIFEST.in` — which is why the DMG stayed correct while the published Linux wheel was broken |

`embeddings._REQUIRED_VENDORED_LIBS` is the single declaration of what must
ship. `test/test_vendored_llama_payload.py` asserts each lane against it, and
both `build.yml` (per PR) and `build-wheel.yml` (release/nightly) re-check the
built wheel **and** sdist by running the shared
`scripts/verify_vendored_payload.py` (one script, so the two lanes cannot drift
apart into a gate that no longer guards). Note `python -m build --wheel` alone
never evaluates `MANIFEST.in`, so a wheel-only build cannot detect an sdist
regression — both CI lanes therefore build the sdist as well.

**When upgrading, add the new version's files to that dict** — the tests verify
the declaration, so a lib that is not declared is a lib nobody notices going
missing.

### macos_x86_64: built from source (no official wheel)

Upstream publishes no macOS x86_64 wheel for 0.3.34 (PyPI, the CPU wheel
index, and GitHub releases are arm64-only for macOS). The dylibs were built
from the sha256-pinned PyPI sdist on macOS under Rosetta with an x86_64
CPython (uv-managed python-build-standalone) and:

```
CMAKE_ARGS="-DCMAKE_OSX_ARCHITECTURES=x86_64 -DGGML_METAL=OFF -DGGML_NATIVE=OFF
  -DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew -DLLAMA_BUILD_COMMON=OFF
  -DLLAMA_OPENSSL=OFF -DHTTPLIB_USE_OPENSSL_IF_AVAILABLE=OFF -DLLAMA_CURL=OFF"
```

`GGML_NATIVE=OFF` keeps the code generic x86-64 (no -march=native).
`LLAMA_BUILD_COMMON=OFF` + the OpenSSL/curl switches drop llama-common (not
part of the shipped closure) and its TLS link against Homebrew's arm64
OpenSSL, which cannot link into an x86_64 build. `CMAKE_IGNORE_PREFIX_PATH`
keeps CMake away from arm64 Homebrew libraries entirely. The same closure as
`macos_arm64/` is extracted (`libllama` + `libggml*`); dylib sha256s:

```
bb86af6801bdb1610784619450f71e00d9ce66b2c018cc847a625431a450c5a4  libggml-base.0.dylib
34cc0d8d3c1d3bb8cdafa51ad986410add2c7196867f2a84f160efa0525540d6  libggml-blas.0.dylib
c165a668db8017323633638311c637036940cdafffdd059ff9ab101ae4e3c3a0  libggml-cpu.0.dylib
5904f53af4cefdb02358a758eaa9b3b8a5ca65bc6a40afba0b629604d015e420  libggml-metal.0.dylib
24646be604824d9fdf107cfb9c63374519754327cf368603cf6f873c75d2341d  libggml.0.dylib
d28ac2ad5d9fbccd6df6622a6aa3cd54ae8474c88b6672c9fb7a59d4a05ed040  libllama.dylib
```

To upgrade: download the four wheels for the new version (plus rebuild
`macos_x86_64/` from the new sdist with the CMAKE_ARGS above), re-extract the
same closure (`libllama` + `libggml*` + vendored `libgomp` on Linux; top-level
dylibs on macOS), replace `llama_cpp/` with the new wheel's Python code (minus
`lib/` and `server/`), and re-run the embedding smoke test in
`test/test_embeddings.py`.
