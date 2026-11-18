# Vendored third-party code

## llama-cpp-python 0.3.34 (MIT)

`llama_cpp/` is the Python package from [abetlen/llama-cpp-python](https://github.com/abetlen/llama-cpp-python)
v0.3.34, vendored so the in-process embedding runtime needs no runtime pip
install, no `--extra-index-url`, and no external Ollama server. License:
`llama_cpp/LICENSE.md` (MIT, includes bundled llama.cpp/ggml — also MIT).

What was changed relative to the upstream wheel:

- `llama_cpp/lib/` (bundled native libs) removed — per-platform libraries live
  in `llama_cpp_libs/<platform>/` instead. `kiro_crew.embeddings` hands the
  selected directory to the loader through a process-local seam (a module it
  registers in `sys.modules` as `_kiro_crew_llama_cpp_lib_path`, carrying
  `LIBS_DIR`; see the divergence in `llama_cpp/llama_cpp.py` below), never
  through the environment: a variable set even for the duration of the import
  is inherited by every child spawned in that window — an MCP server, a
  session, the gateway an in-app restart execs into. The upstream-supported
  `LLAMA_CPP_LIB_PATH` env var remains the operator's override (a GPU build, a
  hand-assembled lib dir), and the embedding loader never writes it. A value
  shaped like ANY install's bundled directory
  (`…/kiro_crew/_vendor/llama_cpp_libs/<platform>`) is read as inherited from
  another Kiro Crew process, not as an operator's choice. Each entry prelude
  removes and records that value on the main thread before any thread or child
  process exists; a process that bypasses those preludes reaches a loader that
  classifies and ignores the value without mutating the environment. Shape alone
  decides, whatever the directory holds, since a complete one may be a
  still-present previous install's: point the override at a directory of your
  own, whose path does not end in that shape.
- `llama_cpp/server/` removed (FastAPI server — unused, heavy deps).
- The Linux `libggml.so.0`, `libggml-base.so.0` and `libggml-cpu.so.0` carry
  private names: each file is renamed `<stem>-<hash>.so.0`, its soname is set
  to that name, and the `libllama.so` and ggml dependency entries name the new
  files (see "Private ggml names on Linux" below). With upstream's public names,
  a user-installed whisper.cpp build in the same process (a source-built
  `pywhispercpp` for GPU speech, say) makes the dynamic loader reuse whichever
  ggml loaded first for both: ours then fails on a symbol their ggml lacks, or
  theirs runs on our CPU-only ggml. `.text` and `.rodata` stay byte-identical
  to the unpatched files; patchelf moves the dynamic strings into an added load
  segment.
- `llama_cpp.llama_cache`'s module-level `import diskcache` is satisfied by a
  `sys.modules` stub installed in `kiro_crew.embeddings._install_diskcache_stub`
  (KiroCrew never uses disk-backed LLM state caching; a real installed
  diskcache, if present, wins).

`llama_cpp_libs/` holds the minimal verified shared-library closure per
platform, extracted from the official prebuilt CPU wheels
(https://abetlen.github.io/llama-cpp-python/whl/cpu) — except `macos_x86_64/`
and `linux_aarch64_dotprod/`, which upstream does not publish and are built
from the pinned PyPI sdist (see below):

| Dir | Source wheel |
|-----|--------------|
| `linux_x86_64/`  | `llama_cpp_python-0.3.34-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` (sha256 f34c26f51ec4fd4e0355c5384b7056f877bf7a38c9d7897a46c78118ca900366) |
| `linux_aarch64/` | `llama_cpp_python-0.3.34-py3-none-manylinux2014_aarch64.manylinux_2_17_aarch64.whl` (sha256 725d8a324032b3f1143c20eee62e47415476eb85127e8a134e3a431d666d21d1) |
| `linux_aarch64_dotprod/` | built from sdist `llama_cpp_python-0.3.34.tar.gz` with `+dotprod` — see below |
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
one WARNING — nothing fails loudly. Two packaging lanes select these files, each
by its own mechanism, and each can drop them alone:

| Lane | Mechanism | Gotcha |
|---|---|---|
| sdist | `MANIFEST.in` | `global-exclude *.so` strips exactly `libllama.so` (other Linux libs end `.so.0`; macOS/Windows use `.dylib`/`.dll`). The re-include MUST stay after every exclude — later rules win. `python -m build` builds the wheel FROM the sdist, so a loss here reaches every pip install |
| wheel | `setup.cfg [options.package_data]` | explicit per-platform globs, because setuptools' `**` recursion has varied across versions |

The desktop bundle has no selection rules of its own: `packaging/build-desktop.sh`
pip-installs the project into the bundled python-build-standalone interpreter, so
it inherits the wheel's `package_data`. It does remove files afterwards: the
Windows bundle's prune step deletes the foreign-platform `llama_cpp_libs/`
directories (`linux_*`, `macos_*`), keeping only `win_amd64/`.

`_llama_lib_path._REQUIRED_VENDORED_LIBS` is the single declaration of what must
ship (`embeddings` re-exports it; it lives in that stdlib-only leaf so the
build-time verifier can read it with `ast` alone).
`test/test_vendored_llama_payload.py` asserts each lane against it, and
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
CMAKE_ARGS="-DCMAKE_OSX_ARCHITECTURES=x86_64 -DGGML_NATIVE=OFF
  -DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew -DLLAMA_BUILD_COMMON=OFF
  -DLLAMA_OPENSSL=OFF -DHTTPLIB_USE_OPENSSL_IF_AVAILABLE=OFF -DLLAMA_CURL=OFF"
```

The Metal backend stays at its default (on): the shipped closure includes
`libggml-metal.0.dylib`, which `embeddings._REQUIRED_VENDORED_LIBS` requires, so a
rebuild must not pass `-DGGML_METAL=OFF`; a builder should confirm the rebuilt
closure still contains that dylib.

`GGML_NATIVE=OFF` avoids `-march=native`, but the enabled upstream x86 CPU
kernels still require AVX, AVX2, BMI2, F16C, FMA, SSE3, and SSSE3. The loader
checks that baseline before using the bundled Linux x86_64 or macOS x86_64
runtime and degrades to keyword search when the host cannot execute it. An operator-set
`LLAMA_CPP_LIB_PATH` bypasses that bundled-runtime gate so a compatible custom
build remains usable.
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

### linux_aarch64_dotprod: built from source (int8 dot product)

Upstream's Linux arm64 wheel targets plain `armv8-a`, so the Q8_0 embedding
model runs without the `sdot` int8 dot-product instruction (`objdump -d` finds
0 in its `libggml-cpu.so.0`; this build has 992). On a 16-core Graviton3 at 8
threads a 2,400-char embed takes 4.2 s on `linux_aarch64/` and 1.5 s here.
Vectors match (cosine ≥ 0.9997 on a fixed text set, the same spread a compiler
change alone gives).

The loader picks this directory only when EVERY `Features` line in
`/proc/cpuinfo` lists `asimddp` and every file below is present
(`embeddings_native._linux_aarch64_libs_dir`); anything else loads
`linux_aarch64/`. On a core without `sdot` (Cortex-A72, Graviton1,
Raspberry Pi 4) this build dies with SIGILL, which nothing can catch.

It is upstream's own arm64 wheel job (`.github/workflows/build-and-release.yaml`
in the sdist: manylinux2014 aarch64 image, `pip wheel`, `auditwheel repair`)
with one flag added. Run on an aarch64 host with Docker:

```
# sdist sha256 d849d286d808284f1d3ec1bd6875572430d29d1f9574a010232caa4e9cef0e35
docker run --rm -v "$PWD:/io" -w /io \
  quay.io/pypa/manylinux2014_aarch64@sha256:ec445e2688785976c727d304f421ae8af5489715c19508526a749716e240541a \
  bash -euxc '
    mkdir src && tar -xzf llama_cpp_python-0.3.34.tar.gz -C src --strip-components 1
    export CMAKE_ARGS="-DGGML_NATIVE=off -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod"
    /opt/python/cp310-cp310/bin/python -m pip wheel --no-deps -w dist ./src
    LD_LIBRARY_PATH=/io/src/llama_cpp/lib auditwheel repair -w wheelhouse dist/*.whl'
```

Toolchain: that image's gcc 10.2.1 (devtoolset-10), OpenMP on (the default, as
upstream). Python version does not touch the native build (the wheel is
`py3-none`); cp310 is used because the image no longer carries upstream's cp38.
From the repaired wheel take `llama_cpp/lib/{libllama.so,libggml.so.0,
libggml-base.so.0,libggml-cpu.so.0}` and `llama_cpp_python.libs/libgomp-d22c30c5.so.1.0.0`
— the same closure as `linux_aarch64/` — then apply the rename step in
"Private ggml names on Linux" below. `llama_print_system_info()` reads
`NEON = 1 | ARM_FMA = 1 | DOTPROD = 1 | LLAMAFILE = 1 | OPENMP = 1 | REPACK = 1`.
Two clean builds gave byte-identical files (the build output, before the
rename step):

```
1bf6689c1efcf92b1c5b20d404eb501774d1933cea78faf0c912b6609cf4332d  libllama.so
7f42cd95daab68bcda30a8d12aa5ed4985650069548e1fbd0bfbc4f05c939a8a  libggml.so.0
e198f95172099605d3310132961f50aed9d4c20aae8f2f8673d927ede8c60e93  libggml-base.so.0
a45ca40cab2825576218359bcee9de11ba46c00216074ace5ba052642a4d3321  libggml-cpu.so.0
bd0f8bc6b22d5cde4365332ba87c1151416bfc6fa4c51c6f669308de507bd35f  libgomp-d22c30c5.so.1.0.0
```

### Private ggml names on Linux

Applied to `linux_x86_64/`, `linux_aarch64/` and `linux_aarch64_dotprod/` after
the closure is extracted, with patchelf 0.17.2 (`pip install patchelf==0.17.2.1`).
The new name is auditwheel's scheme, the one the vendored `libgomp` files
already use: `<stem>-<first 8 hex of the sha256 of the UNPATCHED file>.so.0`.
Each name is taken from its file's sha256 before that file is patched, because
patching changes the bytes. `libllama.so` keeps its file name (ctypes opens it
by base name) and its soname; `libgomp` is untouched. Run in each directory,
with bash:

```
set -euo pipefail
repl=()
for old in libggml.so.0 libggml-base.so.0 libggml-cpu.so.0; do
  new="${old%.so.0}-$(sha256sum "$old" | cut -c1-8).so.0"
  mv "$old" "$new"
  patchelf --set-soname "$new" "$new"
  repl+=(--replace-needed "$old" "$new")
done
for f in libllama.so libggml-*.so.0; do patchelf "${repl[@]}" "$f"; done
```

`--replace-needed` leaves a file that does not need a name unchanged, so the
same list is passed to every file. Check the result: `readelf -d` shows each
`libggml*` soname equal to its file name, no `libggml*.so.0` public name in any
`NEEDED` entry, and the `RPATH` (not `RUNPATH`) value unchanged;
`objcopy -O binary --only-section=.text` (and `.rodata`) of each patched file,
with an objcopy for its architecture (`aarch64-linux-gnu-objcopy` for the arm64
dirs), is byte-identical to the unpatched file's. Two runs from the same inputs
give byte-identical files. `test/test_vendored_llama_payload.py` fails if a
public ggml name, a soname that differs from its file name, or a `RUNPATH`
appears.

The private names keep the two ggml copies apart only because both sides load
them RTLD_LOCAL (ctypes' Linux default in `llama_cpp/_ctypes_extensions.py`,
and Python's for extension modules): the `ggml_*` symbols stay public and
unversioned, so a binding that opened these libraries RTLD_GLOBAL would bring
the collision back by symbol name. The step also assumes every ggml file is
reached through a dependency entry: a runtime that adds a ggml file, or uses
ggml's dynamically loaded backends (`GGML_BACKEND_DL`, which finds
`libggml-<name>-*.so` by file name), needs it revisited.

The source wheel and sdist sha256s above identify the inputs; the shipped bytes
are pinned by git and `scripts/vendor_manifest.sha256`. sha256 before and after
the rename (the after column equals the manifest; re-record both columns on
every upgrade):

| Dir | Before | After |
|-----|--------|-------|
| `linux_x86_64/` | `libllama.so` 9c1b95c4b475cceb91a65e5685befcb798f0921dcc81c28fec8793442bc6ffbb | `libllama.so` 4ea570afcc181e027127c955aa4cc5a70c4a846dab2473ca42d899766e54c4b2 |
| `linux_x86_64/` | `libggml.so.0` f8c1924a465a5cb65beab9a441472ad6404c47da946ea4fb07f0f5b4c3324832 | `libggml-f8c1924a.so.0` b2aac95dfa2da41086d6a72fd70f0fdcfceac5e8105b3d77673c7aeaea0f221c |
| `linux_x86_64/` | `libggml-base.so.0` 5cdc9aad31bf77160b64bd1275a55bec4e809408cd8e26427bd8e035ffffd3b2 | `libggml-base-5cdc9aad.so.0` d67918267231fcf4f35dc2aac76733d078f3f9b8180e5e27a8919dd3c0dad483 |
| `linux_x86_64/` | `libggml-cpu.so.0` c29a841c67b6ac4df1e8acbfd20fab16345eaad816b6e421e3dd2b18456cd97c | `libggml-cpu-c29a841c.so.0` 7295b019b79af98aa55fcfee179f85cb604227eb1d86714a06ae295d9d39197a |
| `linux_aarch64/` | `libllama.so` c9b722bd52645d605f07698dc0d646dd4719588957f76ef6183132387f1cdc45 | `libllama.so` cd6e195169dd95b67227e586161a28f30c9619f693b03d62efbc901e527ff807 |
| `linux_aarch64/` | `libggml.so.0` de95cd8d39ef2f9222681a01a40897c4099aa28460f28e33ef864b4960f95fb5 | `libggml-de95cd8d.so.0` d4e885fe69b8aa3ba8a197c9a1011bbf4d3b790ff35c9c0a0f104f808d48f34b |
| `linux_aarch64/` | `libggml-base.so.0` 6da567d19c7f7e87667e08033c4b81b57c8082842e2b60ba4c825214497ef315 | `libggml-base-6da567d1.so.0` b584d420ee4c6fcdc06ededcba2c3b86554d85f96e3a6f8c9317d28a193e6196 |
| `linux_aarch64/` | `libggml-cpu.so.0` 47ecf749fd96cc4e9ba7a411b08d167a22a97e9b0dd683201e3e7b2bca66a57c | `libggml-cpu-47ecf749.so.0` b06b6fea18bdfce27ca135153e75e90b6694e028be502b9aaee05d181171b69b |
| `linux_aarch64_dotprod/` | `libllama.so` 1bf6689c1efcf92b1c5b20d404eb501774d1933cea78faf0c912b6609cf4332d | `libllama.so` 4d707ea382f90ae5214e2eda7c8b933bf64d7ce248f3fb28df91ef2e649c726e |
| `linux_aarch64_dotprod/` | `libggml.so.0` 7f42cd95daab68bcda30a8d12aa5ed4985650069548e1fbd0bfbc4f05c939a8a | `libggml-7f42cd95.so.0` ac667de95e940ede7d1316022881ba43c2d1d3b71ea597b10ebc4d157b429435 |
| `linux_aarch64_dotprod/` | `libggml-base.so.0` e198f95172099605d3310132961f50aed9d4c20aae8f2f8673d927ede8c60e93 | `libggml-base-e198f951.so.0` a3db2a3bca1edf11f5d403518c9527bb18711ec82b01f0f3afb7b5708f93b395 |
| `linux_aarch64_dotprod/` | `libggml-cpu.so.0` a45ca40cab2825576218359bcee9de11ba46c00216074ace5ba052642a4d3321 | `libggml-cpu-a45ca40c.so.0` b98e5dddebd0a3303da45f9a7b8fc2546a34c3cbdad808659813030f89264d17 |

The `libgomp` files keep their names and bytes.

To upgrade: download the four wheels for the new version (plus rebuild
`macos_x86_64/` from the new sdist with the CMAKE_ARGS above, and
`linux_aarch64_dotprod/` with the recipe above), re-extract the
same closure (`libllama` + `libggml*` + vendored `libgomp` on Linux; top-level
dylibs on macOS), apply the rename step in "Private ggml names on Linux" to the
three Linux directories and update their names in `_REQUIRED_VENDORED_LIBS`,
replace `llama_cpp/` with the new wheel's Python code (minus
`lib/` and `server/`), re-mirror the cache-less architecture set (below), and
re-run the embedding smoke test in `test/test_embeddings.py`.

### Re-mirror `_ENCODER_ARCHITECTURES` on every upgrade

`embeddings._ENCODER_ARCHITECTURES` names the architectures llama.cpp runs
without a KV cache: the `res = nullptr` cases of `llama_model::create_memory`
in the new version's `src/llama-model.cpp` (llama.cpp source, pinned by the
`llama-cpp-python` tag), plus `t5encoder`. Those architectures go through
`encode()`, which aborts the process unless one micro-batch holds every input
token, so a name missing from the set is an uncatchable SIGABRT in an
operator's gateway. On every bump:

1. Open the new `llama_model::create_memory` and copy every `case LLM_ARCH_*`
   that sets `res = nullptr`, spelled as its `LLM_ARCH_NAMES` string, into the
   set (plus `t5encoder`). ADDITIONS are found only by this read — no test can
   tell which switch case a name in the binary belongs to.
2. Run `test/test_encoder_architecture_mirror.py`: it opens every vendored
   `libllama` binary and requires each name in the set as a NUL-terminated
   string, so a REMOVED or RENAMED architecture fails there — except a name
   that is the suffix of another listed name (`bert` inside `modern-bert`):
   GNU ld tail-merges such strings, so only the terminating NUL can be
   required and the longer name still satisfies the check. Confirm those
   names in the `create_memory` read of step 1. Keep the parametrized list
   in `test/test_embeddings.py` in step with the set.

### Local divergences from upstream (re-apply on every upgrade)

The upgrade step above replaces `llama_cpp/` wholesale with the new wheel's
Python code, which would silently drop the patches below. Each is marked in
source with a `kiro_crew DIVERGENCE FROM UPSTREAM` comment, so
`grep -rn 'kiro_crew DIVERGENCE' llama_cpp/` lists what must be re-applied.
That grep cannot find a binary patch: the Linux ggml rename is re-applied by
the step in "Private ggml names on Linux".

- `llama_cpp/llama.py`, `Llama.__init__` — the eager `self.scores` per-token
  logits buffer is allocated with **zero rows** when the model is opened in
  embedding mode with `logits_all` off (issue #6827). Upstream always allocates
  `(n_batch, n_vocab)` float32, which for the shipped Qwen3-Embedding-0.6B is
  ~1.24 GB that the embedding path never reads. `test/test_embed_scores_buffer.py`
  parses this file and fails if the divergence goes missing.
- `llama_cpp/llama.py`, `Llama.embed` and the module-level
  `_kiro_crew_batch_offset` — a bounded decode for last-token pooling. In
  embedding mode llama.cpp makes every token an output and reserves
  `n_vocab + n_embd` floats per output in a native buffer that grows to the
  largest batch ever decoded and never shrinks (1.19 GB after one 2,048-token
  input for the shipped model). When the host sets
  `kiro_crew_max_decode_tokens` on the `Llama` (`kiro_crew.embeddings`'
  `_install_bounded_decode` sets it to the context's `n_ubatch`, for a causal
  context with a KV cache), a LAST-pooled batch longer than that is decoded as
  consecutive slices of at most that many tokens, and each sequence's vector
  is read after the slice holding its last token. At `n_ubatch` each slice is
  exactly one of the micro-batches llama.cpp splits a single call into
  (`split_simple`), so the buffer holds at most `n_ubatch` rows and the
  vectors are unchanged. Unset, the method is upstream's.
  `test/test_embed_bounded_decode.py` runs the shipped method against a fake
  runtime and fails if the divergence goes missing.
- `llama_cpp/llama.py`, `Llama.__init__` — a `use_extra_bufts` keyword (default
  `None`) sets the model param that turns off llama.cpp's CPU weight repacking
  when given. `None` leaves llama.cpp's default, so an unset call is upstream's.
  `__getstate__` carries the field, so a restored `Llama` keeps the choice.
  `kiro_crew.embeddings` passes `False` only when `memory.embedding_weight_repack`
  is `false`. `test/test_embeddings_weight_repack.py` parses this file and fails
  if the divergence goes missing.
- `llama_cpp/llama_cpp.py`, module level — the library directory is resolved
  from the process-local seam first (`sys.modules["_kiro_crew_llama_cpp_lib_path"].LIBS_DIR`,
  registered by `kiro_crew.embeddings._publish_lib_path_for_import`), then from
  `LLAMA_CPP_LIB_PATH`, then upstream's `<package>/lib` default. Without the
  seam the module behaves exactly as upstream. It exists so the host never has
  to put the bundled directory in the environment, where a child spawned during
  the import would inherit it (see the first bullet under "What was changed").
  `test/test_vendored_llama_payload.py::TestVendoredLoaderLibPathPrecedence`
  executes this file's prefix and fails if the order changes or the key drifts.

### Updating the vendored tree (checksum manifest)

Everything under `_vendor/` is excluded from source-level content review
(semgrep, the AI reviewers' diff, and the lint/format configs all skip it), so
CI verifies the tree's CONTENT against a committed checksum manifest instead:
`scripts/vendor_manifest.sha256` pins the SHA-256 of every file here, and the
`vendor-manifest` job in `.github/workflows/fast-gate.yml` fails any PR whose
`_vendor/` contents differ from it — modified, missing, or added files alike.

After a legitimate vendored bump:

1. Verify the downloaded upstream wheel/sdist sha256s against the source
   table above before extracting anything.
2. Regenerate the manifest and commit its diff alongside the vendored
   changes (CI fails otherwise):

   ```
   python scripts/verify_vendor_manifest.py --write
   python scripts/verify_vendor_manifest.py          # confirm a green --check
   ```
3. Re-prove the bounded decode byte-identical on the new runtime. CI has no
   model, so `test_the_byte_identity_proof_covers_the_vendored_runtime` pins
   a digest of the manifest's native-library and embed-path entries and fails
   on the new manifest. Run, with the shipped GGUF (its sha256 is
   `_GGUF_SHA256`):

   ```
   KIROCREW_TEST_EMBED_MODEL=<path to the GGUF> python -m pytest -n0 \
       test/test_embed_bounded_decode.py::TestRealModel
   ```

   and, when it passes, set `_PROVEN_RUNTIME_DIGEST` in that test file to the
   value the failure printed.

The manifest lives outside `_vendor/` on purpose: a manifest change shows up
in the reviewable diff, which is the whole point of the gate. It is
`sha256sum`-compatible, so `sha256sum -c scripts/vendor_manifest.sha256` from
the repository root verifies it independently.
