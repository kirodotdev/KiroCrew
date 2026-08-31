# Updating the vendored ACP v1 schema pin

The gate is pinned to an immutable upstream tag. To bump it:

1. Choose a new **`schema-v1.x.y`** tag — never a `v2.x` (draft wire protocol)
   and never a Rust-crate release tag. The wire protocol has been `1` since it
   stabilised; `schema-v1.x.y` deltas add optional surface, not breaking wire
   changes.
2. Fetch the five files at that tag and overwrite this directory:

   ```bash
   TAG=schema-v1.x.y
   BASE=https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/${TAG}
   for f in LICENSE schema/v1/schema.json schema/v1/schema.unstable.json \
            schema/v1/meta.json schema/v1/meta.unstable.json; do
     curl -fLsS "${BASE}/${f}" -o "$(basename "${f}")"
   done
   ```

3. Verify the tag→commit pin (annotated tags dereference to a commit):

   ```bash
   curl -fsS https://api.github.com/repos/agentclientprotocol/agent-client-protocol/git/refs/tags/${TAG}
   # follow object.url when "type":"tag" to get the commit SHA
   ```

4. Regenerate the integrity manifest:

   ```bash
   sha256sum schema.json schema.unstable.json meta.json meta.unstable.json LICENSE > SHA256SUMS
   ```

5. Update `NOTICE` (Tag/Commit/Retrieved) and `VENDOR.md` (the provenance
   table + SHA256SUMS).
6. Run the gate and the provenance/independence test:

   ```bash
   pytest test/test_acp_v1_conformance_vendor.py \
          test/test_acp_conformance_blackbox.py \
          --override-ini="addopts=-p no:cacheprovider" -n0 -q
   ```

   New rejections on a previously-conformant frame indicate the schema tightened
   or MeshClaw drifted — investigate before landing. If MeshClaw's own
   `kiro_crew.acp.types` no longer matches the pinned vocabulary, the
   independence cross-check in `test_acp_v1_conformance_vendor.py` fails: reconcile
   MeshClaw to the new pin (do not weaken the cross-check to pass).

## Optional: add a real Draft 2020-12 validator / SDK cross-check

If a pinned `jsonschema` (Draft 2020-12) and/or `agent-client-protocol` Pydantic
models become installable, wire them into `test/acp_bb_schema.py` (the single
seam): validate each captured `params`/`result` against the vendored
`schema.json` `$defs`, and optionally cross-check via the SDK. Keep the vendored
files as the primary oracle; the SDK is a secondary cross-check only.
