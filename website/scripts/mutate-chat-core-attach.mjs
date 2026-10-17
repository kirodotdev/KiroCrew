import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'

const output = resolve(process.argv[2] ?? '../temp-screenshots/chat-core-attach-mutations')
mkdirSync(output, { recursive: true })
const cases = [
  ['picker-reset', 'src/chat-core/composer/Attach.tsx', "event.target.value = ''", '// mutation: skip reset'],
  ['picker-writer', 'src/chat-core/composer/Attach.tsx', 'if (files.length) onFiles?.(files)', 'if (false) onFiles?.(files)'],
  ['upload-grant', 'src/app-sdk/useEmbedAttachments.ts', 'if (!granted) {', 'if (false) {'],
  ['files-only', 'src/chat-core/composer/atoms.tsx', 'text.trim().length > 0 || hasAttachments', 'text.trim().length > 0'],
  ['upload-hold', 'src/chat-core/composer/atoms.tsx', 'pending || uploading || !hasContent', 'pending || !hasContent'],
]
let survived = 0
for (const [name, file, from, to] of cases) {
  const original = readFileSync(file, 'utf8')
  if (!original.includes(from)) throw new Error(`Missing mutation anchor: ${name}`)
  try {
    writeFileSync(file, original.replace(from, to))
    const run = spawnSync(process.execPath, ['node_modules/vitest/vitest.mjs', 'run', 'src/chat-core/composer/Attach.test.tsx', 'src/test/ChatEmbed.attachments.test.tsx', '--maxWorkers=2'], { encoding: 'utf8', timeout: 120000 })
    const log = `${run.stdout}\n${run.stderr}`
    writeFileSync(`${output}/${name}.log`, log)
    const caught = run.status !== 0 && /AssertionError|expected .*|TestingLibraryElementError|Received element is not disabled/.test(log) && !run.error
    console.log(`${name}: ${caught ? 'CAUGHT' : 'NOT PROVEN'} (exit ${run.status})`)
    if (!caught) survived++
  } finally {
    writeFileSync(file, original)
  }
}
if (survived) process.exitCode = 1
