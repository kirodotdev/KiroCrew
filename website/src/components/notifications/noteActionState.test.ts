/**
 * `noteActionState`: only a gateway app-update note's Update action opens the
 * app page with its Update flow armed; every other action is a plain link.
 */
import { describe, it, expect } from 'vitest'
import { noteActionState } from './notifMeta'

const update = { id: 'update' }

describe('noteActionState', () => {
  it('arms the update for a system.apps note opening an app page', () => {
    expect(noteActionState({ channel: 'system.apps' }, update, '/apps/detail/demo-notes')).toEqual({ autoAction: 'update' })
  })

  it('leaves a note from any other channel a plain link', () => {
    expect(noteActionState({ channel: 'demo-notes.alerts' }, update, '/apps/detail/demo-notes')).toBeUndefined()
    expect(noteActionState({}, update, '/apps/detail/demo-notes')).toBeUndefined()
  })

  it('leaves another action id a plain link', () => {
    expect(noteActionState({ channel: 'system.apps' }, { id: 'open' }, '/apps/detail/demo-notes')).toBeUndefined()
    // The new-permissions note's "Review update" button only opens the page:
    // that version was held back for consent, so nothing may start on a click.
    expect(noteActionState({ channel: 'system.apps' }, { id: 'review' }, '/apps/detail/demo-notes')).toBeUndefined()
  })

  it('arms nothing for a URL that is not one app page', () => {
    for (const url of ['/settings/about', '/apps/detail/demo-notes?x=1', '/apps/detail/', '/apps/detail/a/b']) {
      expect(noteActionState({ channel: 'system.apps' }, update, url)).toBeUndefined()
    }
  })
})
