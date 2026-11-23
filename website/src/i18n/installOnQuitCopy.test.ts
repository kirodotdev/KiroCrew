/**
 * Before an update is armed, its timing lines name the button that arms it.
 *
 * Only "Install when I quit" (`components.updateModal.install_on_quit`) arms an
 * update on every install: on an externally managed one with automatic
 * downloads off, Later followed by a quit installs nothing. So the ready
 * dialog's timing line and the About card's downloaded note tell the reader to
 * choose that button, by its own label, in every authored locale. Rewording the
 * label alone would leave both lines naming a control that is not on screen.
 *
 * The generated pseudolocale is excluded: it pads every value with its own
 * filler, and it regenerates from `en.json`.
 */
import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const LABEL_KEY = 'components.updateModal.install_on_quit'
const LINE_KEYS = [
  'components.updateModal.install_on_quit_timing',
  'pages.settings.aboutPanel.downloaded_and_verified_installs_on_quit',
]

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

describe('pre-arm update timing lines name Install when I quit', () => {
  const shipped = Object.keys(CATALOGS).filter(code => !GENERATED.has(code))

  it.each(shipped)('%s: each line holds the button label', code => {
    const catalog = (CATALOGS[code] as { translation: Record<string, unknown> }).translation
    const label = resolve(catalog, LABEL_KEY)
    expect(typeof label === 'string' && label.length > 0, `${code} is missing ${LABEL_KEY}`).toBe(true)
    for (const key of LINE_KEYS) {
      const line = resolve(catalog, key)
      expect(typeof line, `${code} is missing ${key}`).toBe('string')
      expect(line as string, `${code} ${key} must name "${label as string}"`).toContain(label as string)
    }
  })
})
