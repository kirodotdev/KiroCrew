import { describe, expect, it } from 'vitest'
import { answerRejectedMessage, answerRestoredNotice } from '../utils/questionAnswers'

// GPT 6.1 on #15250 (errors-use-error-notice, blocking): a submit the answer
// endpoint REJECTED (404: the wait is already gone) is a failure, so it must reach
// the transcript as an error row (ErrorNotice), never as a warn-tone NoticeCard.
// Only a passive retirement with no failed submit may stay a warn notice.
describe('a rejected answer submission', () => {
  it('is an error row carrying the restored-answer text, with no warn prefix', () => {
    const questions = [{ question: 'Which region?' }]
    const message = answerRejectedMessage(questions)
    expect(message.role).toBe('error')
    expect(message.content).toBe(answerRestoredNotice(questions))
    expect(message.content.startsWith('\u26A0')).toBe(false)
  })

  it('still names nothing when the question is unknown', () => {
    expect(answerRejectedMessage(undefined)).toMatchObject({
      role: 'error',
      content: answerRestoredNotice(undefined),
    })
  })

  it('is what both rejected-submit fallbacks post', async () => {
    const pane = (await import('../components/ChatPane.tsx?raw')).default as string
    const busy = (await import('../pages/chat/page/busyTurnControls.ts?raw')).default as string
    // The pane's code also lives in its owner modules; the old notice shape may
    // come back in none of them.
    const paneOwners = (await Promise.all([
      import('../components/ChatPanePickerMenus.tsx?raw'),
      import('../components/ChatPaneProps.ts?raw'),
      import('../components/ChatPaneTranscript.tsx?raw'),
      import('../components/useChatPaneDraft.ts?raw'),
      import('../components/useChatPanePickers.ts?raw'),
      import('../components/useChatPaneSend.ts?raw'),
      import('../components/useChatPaneTurnControl.ts?raw'),
      import('../components/useChatPaneUploads.ts?raw'),
    ])).map(m => m.default as string)
    for (const [src, own] of [[pane, [pane, ...paneOwners].join('\n')], [busy, busy]]) {
      expect(src).toContain('answerRejectedMessage(questions)')
      expect(own).not.toMatch(/role: 'notice', content: '\\u26A0\\uFE0F ' \+ answerRestoredNotice/)
    }
  })
})
