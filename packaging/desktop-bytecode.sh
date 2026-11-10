# shellcheck shell=bash
# Bytecode steps for the POSIX backend trees packaging/build-desktop.sh builds.
# Sourced by that script, which owns the globals these read: ROOT, OS and log.

# Coverage gate: every .py under a tree must ship with a bytecode cache, so a
# tree that shipped without caches cannot silently make every launch want to
# write. A handful of unimportable vendored samples is tolerated; a wholesale
# miss means the compile step did not run.
#   $1 = backend tree   $2 = directory to check   $3 = directory below $2 to
#        skip because a narrower gate owns it ("" = none)
bytecode_coverage_gate() {
  "$1/bin/python3.12" -s - "$2" "$3" <<'COVERAGE'
import os, sys
root, skip = sys.argv[1], sys.argv[2]
missing = []
for dirpath, dirnames, filenames in os.walk(root):
    if os.path.basename(dirpath) == "__pycache__":
        continue
    if skip:
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) != skip]
    cache = os.path.join(dirpath, "__pycache__")
    for name in filenames:
        if not name.endswith(".py"):
            continue
        stem = name[:-3]
        if not any(
            f.startswith(stem + ".") and f.endswith(".pyc")
            for f in (os.listdir(cache) if os.path.isdir(cache) else ())
        ):
            missing.append(os.path.join(dirpath, name))
if missing:
    print(f"    {len(missing)} module(s) shipped without a bytecode cache", file=sys.stderr)
    for path in missing[:10]:
        print(f"      {os.path.relpath(path, root)}", file=sys.stderr)
    if len(missing) > 50:
        print("ERROR: bytecode coverage is too low to forbid runtime writes", file=sys.stderr)
        raise SystemExit(1)
COVERAGE
}

# macOS: ship checked-hash bytecode for the standard library, the kiro_crew
# package and the traced startup closure of its dependencies, and bar the
# bundled interpreter from writing any other.
#   $1 = built backend tree
#
# Why not the whole tree: the signing service reads every file in the app on
# every request (unpack, seal, repack), and its task time follows the size of
# that tree rather than the length of the signing manifest. A cache for every
# module is 37% of the universal app's entries (35.6k of 97.1k in
# 0.12.1-rc.3, about 17.8k per backend); what this ships is about a quarter of
# that.
#
# Any other module is compiled in memory and leaves no file behind,
# because no interpreter running from this tree may write bytecode. Two
# mechanisms make that hold for every process, and each covers what the other
# cannot reach:
#   - precompile_windows.py --ban-bytecode-writes drops a .pth into
#     site-packages that sets sys.dont_write_bytecode. The desktop shell's
#     PYTHONDONTWRITEBYTECODE (website/electron/gateway-env.js) reaches only the
#     gateway's own process tree; the agent runtime starts the MCP stubs and
#     bridges with this interpreter and the Python variables stripped, and
#     `kirocrew` run from a terminal never had them. `site` runs the .pth in
#     every one of those.
#   - The standard library is compiled whole. A child started with -I -S (the
#     sandbox launchers, the rlimit shim, the subprocess pool) skips both the
#     environment and site, so no .pth runs; it can import only the standard
#     library, and every module there already has a cache. The same holds for
#     the few imports that run before site does.
#
# The traced entry points are the gateway import (what Windows traces), the
# CLI, the MCP gateway daemon and stub, and every stdio MCP server the gateway
# can start, each of which would otherwise compile its imports in memory on
# every spawn. kiro_crew itself is compiled whole because a trace of an import
# misses what runs later: a measured gateway boot lazily imports about 450 of
# its modules, which cost about 0.7 s per start when compiled in memory.
# lark_oapi, which Windows also traces, is left out: its ~10.9k modules are
# more than the rest of the backend's caches together. The cost lands only on
# a gateway with the Feishu channel on, which compiles it at every start
# (measured on a Linux x86_64 host: 5.4 s to import without caches against
# 2.5 s with them).
precompile_macos_backend() {
  local out="$1" py sp module servers entry
  local -a trace=() whole=()
  py="$out/bin/python3.12"
  sp="$out/lib/python3.12/site-packages"
  servers="$(env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= "$py" -s -P -c \
    'from kiro_crew.mcp_entry import SLIM_SERVERS; print("\n".join(m for m, _ in SLIM_SERVERS.values()))')"
  for module in kiro_crew.cli_server kiro_crew.cli kiro_crew.mcp_gateway.gatewayd \
      kiro_crew.mcp_gateway.stub kiro_crew.mcp_entry; do
    trace+=( --module "$module" )
  done
  while IFS= read -r module; do
    if [ -n "$module" ]; then trace+=( --module "$module" ); fi
  done <<< "$servers"

  log "Precompiling the macOS startup closure as checked-hash pycs ($(basename "$out"))…"
  env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= \
    "$py" -s -P "$ROOT/packaging/precompile_windows.py" --root "$out" \
    --invalidation-mode checked-hash "${trace[@]}" --ban-bytecode-writes "$sp"

  # The standard library and the kiro_crew package, whole. `-q -f` and the
  # tolerated exit code as on the whole-tree path in build_backend; the coverage
  # gate below is what gates.
  for entry in "$out/lib/python3.12"/*; do
    [ "$entry" = "$sp" ] || whole+=( "$entry" )
  done
  whole+=( "$sp/kiro_crew" )
  log "Precompiling the standard library and kiro_crew as checked-hash pycs ($(basename "$out"))…"
  env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= \
    "$py" -s -m compileall -q -f --invalidation-mode checked-hash "${whole[@]}" \
    || echo "    compileall reported errors (unimportable vendored samples are expected)"

  # Coverage gate, in three parts. A fresh process traces the closure again and
  # fails on any module without a valid checked-hash cache; the standard
  # library and kiro_crew must be covered whole; and an interpreter that never
  # saw PYTHONDONTWRITEBYTECODE must still refuse to write.
  env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= \
    "$py" -s -P "$ROOT/packaging/precompile_windows.py" --root "$out" \
    --invalidation-mode checked-hash "${trace[@]}" --verify \
    || { echo "ERROR: the traced startup closure shipped without valid bytecode caches" >&2; exit 1; }
  bytecode_coverage_gate "$out" "$out/lib" "$sp"
  bytecode_coverage_gate "$out" "$sp/kiro_crew" ""
  env -u PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE=1 PYTHONPATH= "$py" -s -c \
    'import sys; raise SystemExit(0 if sys.dont_write_bytecode else 1)' \
    || { echo "ERROR: the bundled interpreter would write bytecode into the signed bundle" >&2; exit 1; }
}
