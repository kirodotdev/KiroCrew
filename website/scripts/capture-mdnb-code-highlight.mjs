/**
 * Screenshot harness for coloured fenced code blocks in the Notes app.
 *
 * The claim needing evidence is that a labelled fence gets syntax colour
 * WITHOUT costing the app its defining gesture — clicking a block opens the
 * markdown source — and that a bare fence stays exactly as plain as before.
 *
 *   01 rendered - a labelled Python fence (coloured) beside a bare fence
 *                 (plain), both on the shipped default theme
 *   02 editing  - the coloured block clicked open: the raw fenced source
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-code-highlight.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { runMdnbCapture } from './lib/mdnb-capture-harness.mjs'
import { mdnbApiStub, mdnbNoteDoc, mdnbNotesList } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-code-highlight'
mkdirSync(OUT, { recursive: true })

const NOTE_PATH = 'code-demo.md'
const NOTE_TITLE = 'Deploy Runbook'

const NOTE_CONTENT = `# ${NOTE_TITLE}

Redeploy the gateway service.

\`\`\`python
def redeploy(service: str) -> bool:
    # Restart only if health checks are currently green.
    return check_health(service) and restart(service)
\`\`\`

Raw output from the last run, unlabelled on purpose:

\`\`\`
$ kirocrew service status
active (running) since Mon 2026-08-17 09:00:00 UTC
\`\`\`
`

const mdnbApi = mdnbApiStub({
  notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE),
  doc: mdnbNoteDoc(NOTE_PATH, NOTE_CONTENT),
})

await runMdnbCapture({
  out: OUT,
  noteTitle: NOTE_TITLE,
  mdnbApi,
  renderedText: 'def redeploy',
})
