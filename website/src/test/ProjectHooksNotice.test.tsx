import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const setProjectHooksConsent = vi.fn()
const getProjectHooksConsent = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    setProjectHooksConsent: (...a: unknown[]) => setProjectHooksConsent(...a),
    getProjectHooksConsent: (...a: unknown[]) => getProjectHooksConsent(...a),
  },
}))

import ProjectHooksNotice from '../pages/chat/ProjectHooksNotice'
import { isProjectHooksNotice } from '../pages/chat/projectHooksNoticeMatch'
import type { ChatMessage } from '../types'

const row = (state: string, folder = '/work/repo'): ChatMessage =>
  ({
    role: 'notice',
    content: 'ℹ Project hooks are running because you trusted this folder in kiro-cli.',
    meta: {
      kind: 'spec_hooks',
      project_hooks: state,
      project_hooks_folder: folder,
      project_hooks_commands: [{ event: 'PreToolUse', when: 'before a tool runs', command: 'echo guard' }, { bad: 1 }],
    },
  }) as unknown as ChatMessage

describe('ProjectHooksNotice', () => {
  beforeEach(() => {
    setProjectHooksConsent.mockReset()
    getProjectHooksConsent.mockReset()
  })
  afterEach(() => setProjectHooksConsent.mockReset())

  it('says it could not check when the read fails, and keeps the row state', async () => {
    getProjectHooksConsent.mockRejectedValue(new Error('folder is not an existing directory'))
    render(<ProjectHooksNotice message={row('off')} askAgent={false} />)
    await waitFor(() =>
      expect(screen.getByTestId('project-hooks-notice-read-error').textContent).toContain('could not check'),
    )
    expect(screen.getByTestId('project-hooks-notice-toggle').textContent).toBe('Turn on')
  })

  it('never lets an older read undo a newer save', async () => {
    let answerMountRead: (v: { state: string; enabled: boolean }) => void = () => {}
    getProjectHooksConsent
      .mockImplementationOnce(() => new Promise((resolve) => { answerMountRead = resolve }))
      .mockResolvedValue({ state: 'on', enabled: true })
    setProjectHooksConsent.mockResolvedValue({ ok: true, enabled: true })
    render(<ProjectHooksNotice message={row('off')} askAgent={false} />)
    fireEvent.click(screen.getByTestId('project-hooks-notice-toggle'))
    await waitFor(() => expect(screen.getByTestId('project-hooks-notice-toggle').textContent).toBe('Turn off'))
    answerMountRead({ state: 'unset', enabled: false })  // the stale "off" arrives last
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByTestId('project-hooks-notice-toggle').textContent).toBe('Turn off')
  })

  it('shows the real state on a reloaded row, not the state it was written in', async () => {
    // Turned on earlier, then the chat reloaded: the row still says off.
    getProjectHooksConsent.mockResolvedValue({ state: 'on', enabled: true })
    const message = row('off')
    ;(message.meta as Record<string, unknown>).project_hooks_saved_text =
      'ℹ Project hooks are running because you turned them on for this folder.'
    render(<ProjectHooksNotice message={message} askAgent={false} />)
    await waitFor(() => expect(screen.getByTestId('project-hooks-notice-toggle').textContent).toBe('Turn off'))
    expect(document.body.textContent).toContain('running because you turned them on')
    expect(getProjectHooksConsent).toHaveBeenCalledWith('/work/repo')
  })

  it('matches only its own tags', () => {
    expect(isProjectHooksNotice(row('kiro_cli'))).toBe(true)
    expect(isProjectHooksNotice(row('other'))).toBe(false)
    expect(isProjectHooksNotice(row('off', ''))).toBe(false)
    expect(isProjectHooksNotice({ ...row('off'), role: 'assistant' } as ChatMessage)).toBe(false)
  })

  it('turns hooks off for the row folder when kiro-cli started them', async () => {
    setProjectHooksConsent.mockResolvedValue({ ok: true, enabled: false })
    render(<ProjectHooksNotice message={row('kiro_cli')} askAgent={false} />)
    fireEvent.click(screen.getByTestId('project-hooks-notice-toggle'))
    await waitFor(() => expect(screen.getByTestId('project-hooks-notice-saved')).toBeTruthy())
    expect(setProjectHooksConsent).toHaveBeenCalledWith('/work/repo', false)
  })

  it('says the new state next to Saved after a successful Turn on', async () => {
    setProjectHooksConsent.mockResolvedValue({ ok: true, enabled: true })
    const message = row('off')
    ;(message.meta as Record<string, unknown>).project_hooks_saved_text =
      'ℹ Project hooks are running because you turned them on for this folder.'
    render(<ProjectHooksNotice message={message} askAgent={false} />)
    fireEvent.click(screen.getByTestId('project-hooks-notice-toggle'))
    await waitFor(() => expect(screen.getByTestId('project-hooks-notice-saved')).toBeTruthy())
    expect(document.body.textContent).toContain('running because you turned them on')
    expect(document.body.textContent).not.toContain('are off for this folder')
    // The opposite link stays, so the owner can undo the click from the same card.
    const undo = screen.getByTestId('project-hooks-notice-toggle')
    expect(undo.textContent).toBe('Turn off')
    fireEvent.click(undo)
    await waitFor(() => expect(setProjectHooksConsent).toHaveBeenLastCalledWith('/work/repo', false))
    // Back to the row's own state: its own text, and Turn on again.
    await waitFor(() =>
      expect(document.body.textContent).not.toContain('running because you turned them on'),
    )
    expect(document.body.textContent).toContain('trusted this folder in kiro-cli')
    expect(screen.getByTestId('project-hooks-notice-toggle').textContent).toBe('Turn on')
  })

  it('turns hooks on from the off row and shows a refusal', async () => {
    setProjectHooksConsent.mockImplementation(async () => { throw new Error('refused') })
    render(<ProjectHooksNotice message={row('off')} askAgent={false} />)
    fireEvent.click(screen.getByTestId('project-hooks-notice-toggle'))
    await waitFor(() => expect(screen.getByTestId('project-hooks-notice-error').textContent).toContain('Reload the page'))
    expect(screen.getByTestId('project-hooks-notice-error').textContent).not.toContain('refused')
    expect(screen.getByTestId('project-hooks-notice-error-details').textContent).toContain('refused')
    expect(setProjectHooksConsent).toHaveBeenCalledWith('/work/repo', true)
  })

  it('lists each hook event and command, read-only, behind Show commands', () => {
    render(<ProjectHooksNotice message={row('off')} askAgent={false} />)
    const list = screen.getByTestId('project-hooks-notice-commands')
    expect(list.tagName).toBe('DETAILS')
    expect(list.textContent).toContain('before a tool runs')
    expect(list.textContent).toContain('echo guard')
    expect(list.querySelectorAll('li')).toHaveLength(1)
  })
})
