import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { HiddenSessionsBox } from './AppDetailGrantBoxes'

describe('HiddenSessionsBox', () => {
  it('says the app keeps its chats out of the list, and that requests still arrive', () => {
    render(<HiddenSessionsBox />)
    const box = screen.getByTestId('hidden-sessions')
    expect(box.textContent).toContain("Its own chats stay out of your sidebar and session list; open them from the app's page.")
    expect(box.textContent).toContain('Their approvals and questions still reach you.')
    expect(box.querySelector('code')?.textContent).toBe('hiddenSessions')
  })
})
