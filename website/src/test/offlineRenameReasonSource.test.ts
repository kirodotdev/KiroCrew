import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// `OfflineReasonRow` (components/OfflineMenuReason.tsx) is the one neutral
// "held because the gateway is down" line: the artifact table pins it, the
// folder-name field insets it, and the two session-rename hosts below use it.
// Hand-writing a third copy is how the styling drifted apart the first time --
// one host had reached `text-xs` while the shared row renders `text-[11px]` --
// so a fix to one would miss the others.
const PAGES = join(__dirname, '..', 'pages')
const HOSTS = [
  ['the sidebar row', join(PAGES, 'chat-sidebar', 'SessionRow.tsx')],
  ['the header control', join(PAGES, 'chat', 'SessionTitleControl.tsx')],
] as const

describe('the offline rename reason has one source', () => {
  for (const [label, path] of HOSTS) {
    it(`renders ${label}'s refusal through the shared row`, async () => {
      const s = await readFile(path, 'utf8')
      expect(s, 'must import the shared row')
        .toMatch(/import \{ OfflineReasonRow \} from '[^']*components\/OfflineMenuReason'/)
      expect(s, 'must render it with the rename reason')
        .toMatch(/<OfflineReasonRow[\s\S]{0,400}?utils\.offline\.gateway_offline_reconnect/)
      // The glyph is the tell: the shared row carries no icon, so a WifiOff in
      // one of these hosts means a second hand-written notice came back.
      expect(s, 'a WifiOff here means a hand-written copy returned')
        .not.toMatch(/WifiOff/)
    })
  }
})
