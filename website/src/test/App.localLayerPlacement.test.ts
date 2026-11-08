import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// The image viewer and the import-failure notice render in place (fixed
// layers, not portals), so in the dashboard they must sit inside the local
// pane's LocalOverlayRoot to hide with the local Crew, and the popout and
// embed frames, which have no local pane, must keep their own copies.
const APP = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')

describe('App placement of in-place local layers', () => {
  const open = APP.indexOf('<LocalOverlayRoot hidden=')
  const close = APP.indexOf('</LocalOverlayRoot>')

  // The dashboard's copy navigates to /chat on Open; the frames' copies route themselves.
  it.each(['<Lightbox />', '<ImportSessionOutcomeNotice navigateToChat />'])('mounts %s inside the local overlay root', tag => {
    const at = APP.indexOf(tag, open)
    expect(open).toBeGreaterThan(-1)
    expect(at).toBeGreaterThan(open)
    expect(at).toBeLessThan(close)
  })

  it.each(['{(isPopout || isEmbed) && <Lightbox />}', '{(isPopout || isEmbed) && <ImportSessionOutcomeNotice />}'])(
    'keeps %s for the popout and embed frames',
    mount => { expect(APP).toContain(mount) },
  )
})
