#!/bin/sh
# Builds the shim at a path whose tail matches Crew's recycle guard for the Codex adapter
# (@agentclientprotocol/codex-acp/dist/index.js). It must be a real file: Crew resolves symlinks.
set -e
cd "$(dirname "$0")"
OUTPUT="${1:-shim/@agentclientprotocol/codex-acp/dist/index.js}"
PACKAGE_DIR="$(dirname "$(dirname "$OUTPUT")")"
mkdir -p "$(dirname "$OUTPUT")" "$PACKAGE_DIR"
{
  printf '%s\n' '#!/usr/bin/env node'
  printf '%s\n' "process.env.ACP_TAP_CONTENT = process.env.ACP_TAP_CONTENT === '0' ? '0' : '1';"
  printf '%s\n' "process.env.ACP_TAP_PROMPTS = process.env.ACP_TAP_PROMPTS === '0' ? '0' : '1';"
  tail -n +2 acp-tap.js
} > "$OUTPUT"
printf '{"type":"commonjs"}\n' > "$PACKAGE_DIR/package.json"
echo "shim: $OUTPUT"
