/**
 * A gateway note's Open action reads in the dashboard's language; any other
 * action keeps the label its producer sent.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { i18next } from '../../i18n/all'
import { i18nT } from '../../i18n/t'
import type { Notification } from '../../types'
import { noteActionLabel } from './notifMeta'

const note = (i18n?: Notification['i18n']) =>
  ({ ts: '1', kind: 'agent', title: 't', body: 'b', i18n }) as Notification

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('noteActionLabel', () => {
  it("translates a gateway note's open action", async () => {
    await i18next.changeLanguage('de')
    const label = noteActionLabel(note({ id: 'relay_crew_added', params: { name: 'X' } }), { id: 'open', label: 'Open' })
    expect(label).toBe(i18nT('components.notifications.notificationDetailPanel.open'))
    expect(label).not.toBe('Open')
  })

  it("keeps an app note's label and a gateway note's other actions as sent", async () => {
    await i18next.changeLanguage('de')
    expect(noteActionLabel(note(), { id: 'open', label: 'Open' })).toBe('Open')
    expect(noteActionLabel(note({ id: 'relay_crew_added' }), { id: 'report', label: 'View report' })).toBe('View report')
  })
})
