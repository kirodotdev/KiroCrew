import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { EMBEDDABLE_LOOPBACK_HOSTS } from '../components/WebPreviewPanel'

// A cross-LANGUAGE contract, so no type can hold it. The panel frames an
// `http://` preview inside an `https` dashboard only on EMBEDDABLE_LOOPBACK_HOSTS;
// the gateway's CSP admits a frame only on the origins in `_LOOPBACK_FRAME_SRC`.
// A host the panel frames but the CSP drops is a blank frame with no refusal card.
// Subset direction only: `frame-src` is legitimately wider (`0.0.0.0`, https).

/** The space-separated sources of `_LOOPBACK_FRAME_SRC` in server.py. */
function loopbackFrameSrc(): string[] {
  const server = readFileSync(
    join(__dirname, '../../../src/kiro_crew/dashboard/server.py'),
    'utf8',
  )
  const block = /^_LOOPBACK_FRAME_SRC = \(([\s\S]*?)\n\)/m.exec(server)
  expect(block, '_LOOPBACK_FRAME_SRC literal not found in server.py').not.toBeNull()
  const text = [...block![1].matchAll(/"([^"]*)"/g)].map(m => m[1]).join('')
  return text.split(/\s+/).filter(Boolean)
}

describe('Web Preview embeddable hosts vs gateway frame-src', () => {
  it('every embeddable host has a wildcard-port http entry in frame-src', () => {
    const sources = new Set(loopbackFrameSrc())
    const missing = [...EMBEDDABLE_LOOPBACK_HOSTS].filter(h => !sources.has(`http://${h}:*`))
    expect(missing).toEqual([])
  })
})
