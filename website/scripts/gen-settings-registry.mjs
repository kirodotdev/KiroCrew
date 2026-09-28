#!/usr/bin/env node
/**
 * Generate the settings registry from settings panel source files.
 * Usage: node scripts/gen-settings-registry.mjs
 *
 * TWO artifacts come out of one extraction pass:
 *  - `src/components/commandPalette/settingsRegistry.gen.ts` — the UI registry
 *    behind settings search and the command palette's deep links.
 *  - `../src/kiro_crew/docs/settings-registry.generated.json` — the same
 *    controls as the AGENT sees them (id/label/tab/route), bundled into the
 *    Python docs package so a deployed gateway can answer "how do I turn on X?"
 *    with a link instead of prose. Scheme: that directory's
 *    settings-deeplink.md.
 * One pass, so the agent's enumeration can never describe a different set of
 * controls than the dashboard renders.
 */
import { execSync } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')

const runnerScript = `
import { extractAll, generateAgentRegistryJson, generateRegistrySource } from './scripts/settingsExtract'
import * as path from 'path'
import * as fs from 'fs'

const settingsDir = path.resolve(${JSON.stringify(ROOT)}, 'src/pages/settings')
const { entries, skipped } = extractAll(settingsDir)

// Minimum-count floor. The extractor matches JSX by regex, so a change to how labels
// are written (e.g. the i18n conversion swapping \`label="X"\` for
// \`label={i18nT('k')}\`) can silently stop matching and emit an EMPTY registry —
// which takes ALL of settings search down with no error anywhere. That happened
// once; this makes it loud instead. Raise the floor only alongside a real
// increase in settings count.
const FLOOR = 90
if (entries.length < FLOOR) {
  console.error(
    \`Refusing to write registry: extracted only \${entries.length} entries \` +
    \`(floor \${FLOOR}, \${skipped} skipped). The extractor's JSX patterns have \` +
    \`likely stopped matching — see extractStringProp in scripts/settingsExtract.ts.\`
  )
  process.exit(1)
}

const outPath = path.resolve(${JSON.stringify(ROOT)}, 'src/components/commandPalette/settingsRegistry.gen.ts')
const source = generateRegistrySource(entries)
fs.writeFileSync(outPath, source)

// The agent-facing sibling, written only after the floor check above: an
// extractor that stopped matching must not be able to replace a good bundled
// enumeration with an empty one.
const agentOutPath = path.resolve(
  ${JSON.stringify(ROOT)},
  '../src/kiro_crew/docs/settings-registry.generated.json',
)
fs.writeFileSync(agentOutPath, generateAgentRegistryJson(entries))

console.log(\`Generated \${entries.length} entries (\${skipped} dynamic labels skipped) → settingsRegistry.gen.ts + settings-registry.generated.json\`)
`

const tmpFile = path.join(ROOT, '.gen-settings-runner.ts')
fs.writeFileSync(tmpFile, runnerScript)

try {
  execSync(`npx vite-node "${tmpFile}"`, { cwd: ROOT, stdio: 'inherit' })
} finally {
  fs.unlinkSync(tmpFile)
}
