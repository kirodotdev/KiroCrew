// The title-only card's one mark for a folded code-channel crew: each line
// state keeps the status line's words as its label.
import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'
import { codeChannelCrewMark } from '../pages/chat-sidebar/CodeChannelCrewRow'
import type { CrewState } from '../pages/chat-sidebar/codeChannelCrew'

const st = (s: Partial<CrewState> & Pick<CrewState, 'key'>): CrewState => ({ count: 1, detail: '', ...s })

describe('codeChannelCrewMark', () => {
  it.each([
    [st({ key: 'needs_you', approval: true }), i18nT('pages.chatSidebar.needs_approval')],
    [st({ key: 'needs_you' }), i18nT('pages.chatSidebar.code_channel_crew_waiting')],
    [st({ key: 'stuck', count: 2 }), i18nT('pages.chatSidebar.code_channel_crew_stuck', { count: 2 })],
    [st({ key: 'interrupted', count: 3 }), i18nT('pages.chatSidebar.code_channel_crew_interrupted', { count: 3 })],
    [st({ key: 'done', count: 2, total: 5 }), i18nT('pages.chatSidebar.code_channel_crew_done', { count: 2, total: 5 })],
    [st({ key: 'done', total: 0 }), i18nT('pages.chatSidebar.code_channel_crew_done_plain')],
    [st({ key: 'done' }), i18nT('pages.chatSidebar.code_channel_crew_done_plain')],
    [st({ key: 'working', count: 4 }), i18nT('pages.chatSidebar.crew_attention_working', { count: 4 })],
  ])('labels %o', (state, label) => {
    const mark = codeChannelCrewMark(state, 12)
    expect(mark.label).toBe(label)
    expect(mark.glyph).toBeTruthy()
  })
})
