/** A link in a rendered message to a page of this dashboard opens in place,
 * through the in-app navigator (`utils/internalNav`), instead of in a new tab.
 * Anything the navigator refuses, any modified click, and any window with no
 * navigator installed keeps the anchor's own new-tab open. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import '../i18n/all'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { setInternalNavHandler } from '../utils/internalNav'

const OPEN_KEY = 'chat-1-1784661000'
const ACTIVE_KEY = 'chat-2-1784661100'

let uninstall: (() => void) | null = null
afterEach(() => { uninstall?.(); uninstall = null })

function install() {
  const navigate = vi.fn()
  uninstall = setInternalNavHandler({ origins: [], open: navigate })
  return navigate
}

/** Click the link named `name`; true when its default (the new-tab open) was cancelled. */
function clickLink(name: string, init: MouseEventInit = {}): boolean {
  const link = screen.getByRole('link', { name })
  return !fireEvent.click(link, { button: 0, ...init })
}

describe('in-app links in a message', () => {
  it('opens a settings link in place and cancels the new tab', () => {
    const navigate = install()
    render(<MarkdownRenderer content={'See the [rules](/settings/security/rules?highlight=security.denied-commands).'} />)
    // The anchor keeps its new-tab target, so a modified click still gets a tab.
    expect(screen.getByRole('link', { name: 'rules' })).toHaveAttribute('target', '_blank')
    expect(clickLink('rules')).toBe(true)
    expect(navigate).toHaveBeenCalledWith('/settings/security/rules?highlight=security.denied-commands')
  })

  it('opens a link written with a nested label in place', () => {
    const navigate = install()
    render(<MarkdownRenderer content={'Open [the *schedule* page](/schedule).'} />)
    fireEvent.click(screen.getByText('schedule'), { button: 0 })
    expect(navigate).toHaveBeenCalledWith('/schedule')
  })

  it.each([['metaKey'], ['ctrlKey'], ['shiftKey'], ['altKey']])('leaves a %s click to the browser', (key) => {
    const navigate = install()
    render(<MarkdownRenderer content={'[sched](/schedule)'} />)
    expect(clickLink('sched', { [key]: true })).toBe(false)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('leaves an external link and a refused dashboard path to the browser', () => {
    const navigate = install()
    render(<MarkdownRenderer content={'[ext](https://example.test/schedule) and [proj](/projects?run=1)'} />)
    expect(clickLink('ext')).toBe(false)
    expect(clickLink('proj')).toBe(false)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('opens in a new tab as before when no navigator is installed', () => {
    render(<MarkdownRenderer content={'[sched](/schedule)'} />)
    expect(clickLink('sched')).toBe(false)
  })

  it('leaves an open session link to the session switcher', () => {
    const navigate = install()
    const onSessionOpen = vi.fn()
    const sessions = new Map([[OPEN_KEY, 'Open one'], [ACTIVE_KEY, 'Here']])
    render(
      <MarkdownRenderer
        content={`[that run](/chat?sid=${OPEN_KEY})`}
        onSessionOpen={onSessionOpen}
        sessions={sessions}
        activeSession={ACTIVE_KEY}
      />,
    )
    expect(clickLink('that run')).toBe(true)
    expect(onSessionOpen).toHaveBeenCalledWith(OPEN_KEY)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('leaves an artifact link to the artifact panel when one is wired', () => {
    const navigate = install()
    const onArtifactOpen = vi.fn()
    render(<MarkdownRenderer content={'[report](/artifacts/q3-report)'} onArtifactOpen={onArtifactOpen} />)
    expect(clickLink('report')).toBe(true)
    expect(onArtifactOpen).toHaveBeenCalledWith('q3-report')
    expect(navigate).not.toHaveBeenCalled()
  })
})
