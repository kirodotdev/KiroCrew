#!/usr/bin/env node
/**
 * Generate settingsRegistry.gen.ts from settings panel source files.
 * Usage: node scripts/gen-settings-registry.mjs
 */
import { execSync } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')

const runnerScript = `
import { extractAll, generateRegistrySource } from './scripts/settingsExtract'
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
console.log(\`Generated \${entries.length} entries (\${skipped} dynamic labels skipped) → settingsRegistry.gen.ts\`)
`

const tmpFile = path.join(ROOT, '.gen-settings-runner.ts')
fs.writeFileSync(tmpFile, runnerScript)

try {
  execSync(`npx vite-node "${tmpFile}"`, { cwd: ROOT, stdio: 'inherit' })
} finally {
  fs.unlinkSync(tmpFile)
}
