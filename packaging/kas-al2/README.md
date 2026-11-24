# KAS on Amazon Linux 2 (glibc 2.26)

The stock KAS bundle needs glibc 2.27+ in two places: Node 22 and `libonnxruntime.so.1` from `onnxruntime-node` 1.21.0. The scripts here rebuild those pieces on Amazon Linux 2 so the bundle runs there and on every newer Linux.

## When to rebuild

| Change | Node | onnxruntime | Then |
|---|---|---|---|
| KAS engine bump, same Node and `onnxruntime-node` | keep | keep | re-run the publish with both overlays |
| Node patch release (new `-node<N>` in the pin) | rebuild | keep | publish |
| KAS moves to another `onnxruntime-node` version | keep | rebuild (bump `ORT_VERSION` and its pins) | publish |
| Node major (22 leaves maintenance 2027-04-30) | rebuild | rebuild (new Node-API headers) | publish |
| linux-arm64 | separate builds | separate builds | not done yet; arm64 stays at glibc 2.28 |

## Node (linux-x64)

`build-node.sh` builds the Node the pin names (the `-node<N>` part of `KAS_PIN_VERSION` in `src/kiro_crew/acp/kas_pin.py`) inside a pinned `amazonlinux:2` container. It uses AL2's `gcc10` package (GCC 10.5 with binutils 2.35, which assembles simdutf's AVX-512 `vpcompressb`; AL2's default binutils 2.29 does not) and `./configure --partly-static`, so `libstdc++` and `libgcc` are linked in statically. No version suffix is added: `node --version` prints exactly `v<N>`, which the installer requires.

```bash
packaging/kas-al2/build-node.sh build --work /path/to/scratch --out /path/to/out --jobs 32
packaging/kas-al2/build-node.sh verify --out /path/to/out
```

The source is trusted through nodejs.org's `SHASUMS256.txt`. Its detached signature must verify against a key from [nodejs/release-keys](https://github.com/nodejs/release-keys) at a pinned commit. That check runs in a pinned `amazonlinux:2023` container, because AL2's gpg 2.0 cannot read the EdDSA keys some releasers use. The build then checks the source tarball against the verified list.

`build` writes `node-v<N>-linux-x64.tar.xz` (laid out like nodejs.org's tarball, ready for `--node-linux-x64`) plus `.sha256`, `glibc-check.txt`, `provenance.txt` and `build.log`. It fails if `bin/node` needs glibc above 2.26, has a `GLIBCXX_`/`CXXABI_` symbol, links `libstdc++`/`libgcc_s`, or prints another version. `verify` runs `env -i node --version` on `amazonlinux:2` and `amazonlinux:2023`. For the onnxruntime checks with this Node, pass its `bin/node` to `build-onnxruntime.sh verify --al2-node`.

## onnxruntime (linux-x64)

`build-onnxruntime.sh` builds onnxruntime 1.21.0 (CPU execution provider only) and its Node-API binding inside a pinned `amazonlinux:2` container. It links the C++ runtime statically (`-static-libstdc++ -static-libgcc`), so neither file needs `libstdc++` or `libgcc_s`, and the highest glibc symbol is at most 2.26.

```bash
packaging/kas-al2/build-onnxruntime.sh build --work /path/to/scratch --out /path/to/out --jobs 32
packaging/kas-al2/build-onnxruntime.sh verify --out /path/to/out --bundle-x64 /path/to/kas-linux-x64 \
  --stock-node /path/to/node-v22.23.4-linux-x64/bin/node --al2-node /path/to/al2-node
```

`build` needs only docker on the host (root is used inside the container only). Most of a first run is building GCC; the toolchain is cached in `--work` for reruns. Files the container creates are handed back to the host user when it exits; if a run is killed hard, remove `--work` with `docker run --rm -v <work>:/w amazonlinux:2 rm -rf /w/*`. It writes `onnxruntime-node-1.21.0-al2-x64.tar.xz` plus `.sha256`, `glibc-check.txt`, `provenance.txt` and `build.log` to `--out`, and fails if a file needs glibc above 2.26, has a `GLIBCXX_`/`CXXABI_` symbol, or links `libstdc++`/`libgcc_s`.

The tarball is a drop-in for `node_modules/onnxruntime-node/bin/napi-v3/linux/x64/`. It holds `libonnxruntime.so.1`, the `libonnxruntime.so.1.21.0` symlink and `onnxruntime_binding.node`, the same names the bundle ships. The binding finds the library through its `$ORIGIN` rpath, so keep them side by side. `libonnxruntime_providers_shared.so` is not rebuilt: the stock one needs only glibc 2.2.5.

`verify` overlays the tarball on a copy of the bundle's `onnxruntime-node`, then in `env -i` containers: loads the binding with the AL2 Node (`--al2-node`, the one `build-node.sh` makes) on `amazonlinux:2`, checks the bundle's stock Node still fails there, and runs one inference (`onnxruntime/test/testdata/mul_1.onnx`) with the AL2 Node on `amazonlinux:2` and `amazonlinux:2023`, and with the stock Node on `amazonlinux:2023`.

How inputs are trusted:

- onnxruntime is fetched at commit `e0b66cad282043d4377cea5269083f17771b6dfc` (tag `v1.21.0`). `github.com/microsoft/onnxruntime` is not reachable, so the source comes from a fork that carries the upstream tag (`ORT_GIT_URL`, default `ROCm/onnxruntime`); the script checks both `HEAD` and the tag equal that commit, and the stock bundle library reports the same commit in its build info.
- CMake fetches each dependency with the SHA1 in `cmake/deps.txt`. eigen's GitLab zip drifts from its pinned SHA1, so the script never edits the hash: it requires the zip to equal a git checkout of the pinned eigen commit file for file, then builds from that checkout.
- GCC 13.4.0, binutils 2.42 (needed for the AVX-VNNI `vpdpbusds` instruction in MLAS), CMake 3.31.12, Python 3.12.16 (build-time only, ORT's CMake needs 3.10+), the Node 22.23.4 headers and `node-addon-api` are all checked against sha256 pins. The script records where each pin came from.
- The binding is built from upstream's unmodified `js/node/CMakeLists.txt` with `napi_build_version=6`, the same value upstream's `js/node/script/build.ts` uses.

## Publishing an AL2 release

`scripts/kas_bundle_publish.py` takes the two linux-x64 pieces, each with the sha256 you expect:

```bash
python3 scripts/kas_bundle_publish.py --kas-dir <KAS build drop> --version 0.66.117-node22.23.4-al2 \
  --node-version 22.23.4 --out ./publish \
  --node-linux-x64 node-v22.23.4-linux-x64.tar.xz --node-linux-x64-sha256 <hex> \
  --onnx-overlay-linux-x64 onnxruntime-node-1.21.0-al2-x64.tar.xz --onnx-overlay-sha256 <hex>
```

- `--node-linux-x64` must be laid out like nodejs.org's tarball (`node-v<N>-linux-x64/bin/node`) and print exactly `v<N>`; run the publish on a linux-x64 host so that can be checked. Every other Node key still comes from nodejs.org.
- `--onnx-overlay-linux-x64` re-packs only the linux-x64 engine tarball. Every other platform's tar bytes stay the KAS build's.
- `provenance.json` beside `manifest.json` records where each piece came from.
- Name it `<KAS_PIN_VERSION>-al2`; a published version is never overwritten.

## Which host runs which pin

| Host | Pin | glibc floor |
|---|---|---|
| linux-x64, glibc below 2.28 | `KAS_PIN_AL2_VERSION` (`<KAS_PIN_VERSION>-al2`) | 2.26 |
| linux-x64, glibc 2.28+ or unreadable | `KAS_PIN_VERSION` | 2.28 |
| linux-arm64 | `KAS_PIN_VERSION` | 2.28 |
| macOS, Windows | `KAS_PIN_VERSION` | none |

Both pins are the same engine release, so every engine version check reads one value (`KAS_PIN_ENGINE_RELEASE`). The choice is made again on every install check: a host that moves to glibc 2.28+ installs the normal pin next time. `--unpinned`, `--from` with another version and `KIROCREW_KAS_VERSION` are held to 2.28.

To move both pins, publish the normal release and its `-al2` republish, then change `KAS_PIN_VERSION`, `KAS_PIN_MANIFEST_SHA256` and `KAS_PIN_AL2_MANIFEST_SHA256` in `src/kiro_crew/acp/kas_pin.py` in one commit. `KAS_PIN_AL2_VERSION` follows `KAS_PIN_VERSION` by itself.
