/**
 * "Allow for this session" names where the person ends it, in the UI's own labels.
 *
 * Every `components.denyOverride.session.*` row ends "until you close this chat
 * (<Session options> → <Close session>) or {{productName}} restarts": the
 * parenthesis is the path to the control that ends the session, the chat title
 * menu (`pages.chatPage.session_options`) and its Close item
 * (`components.sessionActionsMenu.close_session`). If any of these strings is
 * reworded alone, the row would point at a control that is not on screen; this
 * guard keeps them in step in every authored locale.
 */
import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

describe('deny override session rows name the path to Close session', () => {
  it('every session row in every locale quotes Session options → Close session once, in parentheses', () => {
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      if (lang === 'en-XA') continue
      const label = resolve(catalog.translation, 'components.sessionActionsMenu.close_session')
      const menu = resolve(catalog.translation, 'pages.chatPage.session_options')
      expect(typeof label, `${lang} close_session`).toBe('string')
      expect(typeof menu, `${lang} session_options`).toBe('string')
      const rows = resolve(catalog.translation, 'components.denyOverride.session') as Record<string, string>
      expect(Object.keys(rows).length, `${lang} session rows`).toBe(13)
      for (const [key, value] of Object.entries(rows)) {
        const quoted = new RegExp(`[(（]${escape(menu as string)} → ${escape(label as string)}[)）]`, 'g')
        expect(value.match(quoted)?.length ?? 0, `${lang} session.${key}: ${value}`).toBe(1)
      }
    }
  })
})
