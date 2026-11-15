import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { HiddenSessionsBox } from './AppDetailGrantBoxes'

describe('HiddenSessionsBox', () => {
  it('says the app keeps its chats out of the list, and that requests still arrive', () => {
    render(<HiddenSessionsBox />)
    const box = screen.getByTestId('hidden-sessions')
    expect(box.textContent).toContain("Chats it creates as its own private chats stay out of your sidebar and session list; open them from the app's page.")
    expect(box.textContent).toContain('Its other chats stay listed under the app.')
    expect(box.textContent).toContain('Approvals and questions from hidden chats still reach you.')
    expect(box.querySelector('code')?.textContent).toBe('hiddenSessions')
  })
})
