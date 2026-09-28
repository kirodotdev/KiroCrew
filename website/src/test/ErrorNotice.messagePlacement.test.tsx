/**
 * ErrorNotice `messagePlacement`.
 *
 * A notice whose `message` is a raw server string ("config store unavailable")
 * keeps it as the message because that string is the errorReport journal key
 * the hand-off reads. `messagePlacement="below"` lets the plain-language title
 * lead and puts the raw string under it, smaller and secondary, in both
 * variants -- and the default keeps every existing consumer's shape byte for
 * byte (the block variant's bare text node included).
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ErrorNotice from '../components/ErrorNotice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const RAW = 'config store unavailable (503)'
const navigated: string[] = []

beforeEach(() => {
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  navigated.length = 0
  sessionStorage.clear()
  installSoftNavigate(to => { navigated.push(to) })
})

afterEach(() => { __resetNavSeamForTests() })

describe('ErrorNotice messagePlacement', () => {
  it('default: the block variant runs title and message as one sentence, the message a bare text node', () => {
    render(<ErrorNotice title="Folder order could not be read" message={RAW} testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.textContent).toBe(`Folder order could not be read ${RAW}`)
    // No span wraps the message: the shape every existing consumer's tests read.
    expect([...notice.querySelectorAll('span')].some(el => el.textContent === RAW)).toBe(false)
  })

  it('below: the block variant puts the message on its own smaller line under the title', () => {
    render(<ErrorNotice title="Folder order could not be read" message={RAW} messagePlacement="below" testId="n" />)
    const notice = screen.getByTestId('n')
    const raw = [...notice.querySelectorAll('span')].find(el => el.textContent === RAW)
    expect(raw).toBeTruthy()
    expect(raw!.className).toContain('block')
    expect(raw!.className).toContain('text-[12px]')
    expect(raw!.className).toContain('font-normal')
    // The title still leads, and the alert still reads both to assistive tech.
    expect(notice.textContent?.indexOf('could not be read')).toBeLessThan(notice.textContent?.indexOf(RAW) ?? -1)
    expect(notice).toHaveAttribute('role', 'alert')
  })

  it('below: the inline variant wraps the row so the message takes the line under the title', () => {
    render(<ErrorNotice variant="inline" title="Folder order could not be read" message={RAW} messagePlacement="below" testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.className).toContain('flex-wrap')
    const raw = [...notice.querySelectorAll('span')].find(el => el.textContent === RAW)
    expect(raw!.className).toContain('basis-full')
    expect(raw!.className).toContain('text-[11px]')
  })

  it('below without a title is the default shape: there is nothing to sit under', () => {
    render(<ErrorNotice message={RAW} messagePlacement="below" testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.textContent).toBe(RAW)
    expect([...notice.querySelectorAll('span')].some(el => el.textContent === RAW)).toBe(false)
  })

  it('below keeps the raw string as the journal key: the hand-off still recovers the structured report from it', async () => {
    recordError({ source: 'api', message: RAW, status: 503, code: 'config_store_unavailable', endpoint: '/api/config/kirocrew' })
    render(<ErrorNotice title="Folder order could not be read" message={RAW} messagePlacement="below" askAgent actionPlacement="below" testId="n" />)
    const handoff = screen.getByRole('button', { name: /ask the agent/i })
    // Rendered inside the text column, under the demoted line.
    expect(screen.getByTestId('n').querySelector('.flex-1')?.contains(handoff)).toBe(true)
    await userEvent.click(handoff)
    expect(navigated).toEqual(['/chat'])
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain(RAW)
    expect(staged).toContain('config_store_unavailable')
    expect(staged).toContain('/api/config/kirocrew')
  })
})
