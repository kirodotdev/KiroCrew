/**
 * Conversation-attention copy across every shipped catalog.
 *
 * The two native-toast bodies ("Response ready" / "Waiting for your input")
 * and the two renamed sound categories ("Conversation handoffs" / "Approvals
 * and questions") carry the whole meaning of the notification fix: a toast
 * that reads the same for a finished reply and a pending question, or two
 * category rows a translator collapsed into one wording, silently re-creates
 * the defect in that language while English stays green. `catalogParity`
 * proves the keys exist; this pins that their VALUES still tell the cases
 * apart, in every language, without asserting any particular translation.
 *
 * Imports the full catalog map, so this file pays that load once (see
 * website/docs/testing.md, "What a setupFiles entry costs").
 */
import { describe, it, expect } from 'vitest'
import { CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'

const TOAST_DONE = 'hooks.useWebSocket.response_ready'
const TOAST_INPUT = 'hooks.useWebSocket.waiting_for_input'
const TURN_LABEL = 'pages.settings.notificationsPanel.category_turn'
const TURN_DESCRIPTION = 'pages.settings.notificationsPanel.category_turn_description'
const APPROVAL_LABEL = 'pages.settings.notificationsPanel.category_approval'
const APPROVAL_DESCRIPTION = 'pages.settings.notificationsPanel.category_approval_description'

/** English wording the fix retired; no shipped catalog may still carry it. */
const RETIRED_ENGLISH = ['Agent replies', 'When the agent finishes a turn in any chat', 'Tool approval requests']

function leaf(catalog: Record<string, unknown>, dotted: string): string {
  const value = dotted.split('.').reduce<unknown>(
    (node, part) => (node !== null && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
    catalog,
  )
  expect(typeof value, `${dotted} must be a string`).toBe('string')
  return (value as string).trim()
}

describe('conversation attention copy', () => {
  it.each(SUPPORTED_LANGUAGES.map(l => l.code))('%s keeps the handoff cases distinguishable', code => {
    const catalog = CATALOGS[code].translation
    const done = leaf(catalog, TOAST_DONE)
    const input = leaf(catalog, TOAST_INPUT)
    const turn = leaf(catalog, TURN_LABEL)
    const turnDescription = leaf(catalog, TURN_DESCRIPTION)
    const approval = leaf(catalog, APPROVAL_LABEL)
    const approvalDescription = leaf(catalog, APPROVAL_DESCRIPTION)

    for (const value of [done, input, turn, turnDescription, approval, approvalDescription]) {
      expect(value.length).toBeGreaterThan(0)
    }
    // A finished reply and a pending question must not read the same.
    expect(input).not.toBe(done)
    // The two sound rows must not share a label or a description.
    expect(turn).not.toBe(approval)
    expect(turnDescription).not.toBe(approvalDescription)
    // The retired wording promised audio on every turn / approvals only.
    if (code !== 'en') {
      for (const retired of RETIRED_ENGLISH) {
        expect([turn, turnDescription, approval, approvalDescription]).not.toContain(retired)
      }
    }
  })

  it('English states the specified semantics', () => {
    const en = CATALOGS.en.translation
    expect(leaf(en, TURN_LABEL)).toBe('Conversation handoffs')
    expect(leaf(en, TURN_DESCRIPTION)).toBe('When a conversation finishes or pauses for your input')
    expect(leaf(en, APPROVAL_LABEL)).toBe('Approvals and questions')
    expect(leaf(en, APPROVAL_DESCRIPTION)).toBe('When the agent needs a tool approval or an answer')
    expect(leaf(en, TOAST_INPUT)).toBe('Waiting for your input')
    expect(leaf(en, TOAST_DONE)).toBe('Response ready')
  })
})
