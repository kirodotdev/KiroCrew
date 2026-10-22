/**
 * The card that lets a crewmate ask for tools, and the Profile switch that does
 * the same, only ever ALLOW asking: each tool still needs the user's approval.
 * Every locale says so, and none implies a tool is added without asking.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const DIR = join(__dirname, 'locales')
/** "still needs your approval", per locale. */
const APPROVAL: Record<string, RegExp> = {
  'en.manual': /still needs your approval/,
  'en-XA': /./,
  de: /weiterhin deine Zustimmung/,
  es: /sigue necesitando tu aprobación/,
  fr: /toujours ton accord/,
  it: /comunque la tua approvazione/,
  pt: /ainda precisa da sua aprovação/,
  ru: /требует твоего одобрения/,
  ja: /引き続きあなたの承認が必要/,
  ko: /여전히 승인이 필요/,
  'zh-CN': /仍需你批准/,
  hi: /तुम्हारी मंज़ूरी चाहिए/,
  bn: /তোমার অনুমোদন লাগবে/,
}

describe('tool-access copy', () => {
  for (const [locale, approval] of Object.entries(APPROVAL)) {
    it(`${locale}: the card and the switch say each tool still needs approval`, () => {
      const file = readdirSync(DIR).find(f => f === `${locale}.json`)
      expect(file).toBeTruthy()
      const d = JSON.parse(readFileSync(join(DIR, file as string), 'utf8'))
      const cc = d.components.changeCards
      const mp = d.pages.membersPage
      for (const text of [cc.tool_access_title, cc.dynamic_capabilities_hint, mp.profile_dynamic_capabilities_hint]) {
        expect(typeof text).toBe('string')
        expect(text).toMatch(approval)
      }
      if (locale.startsWith('en')) {
        for (const text of [cc.tool_access_title, cc.field_dynamic_capabilities, cc.dynamic_capabilities_hint, mp.profile_dynamic_capabilities_switch, mp.profile_dynamic_capabilities_hint]) {
          expect(text).not.toMatch(/\b(grant|grants|automatic|automatically|silently|without asking)\b/i)
          expect(text).not.toMatch(/\blet (it|\{\{name\}\}) add\b/i)
        }
      }
    })
  }
})
