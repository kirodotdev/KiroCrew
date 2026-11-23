/**
 * The approval card's header on a narrow pane (a 320px screen): the status
 * pill, the heading and, on a Shared Crew, who raised the request share one
 * row only where they fit. jsdom lays nothing out, so this pins the classes
 * that let the row wrap; capture/check-approval-narrow.mjs measures the real
 * widths in Chromium per language (it failed with 80 findings without them).
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { defaultProps, openTrust, stateWithApproval } from './approvalCardFixtures'
import { COMMAND_WRAP } from '../components/chat-input/CuedScrollBox'

vi.mock('../pages/chat/shared-crew/useRaisedBy', () => ({ useRaisedBy: () => 'Alexandra Montgomery-Fairweather' }))
vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

const classes = (el: HTMLElement) => el.className.split(/\s+/)

describe('the approval card header on a narrow pane', () => {
  it('wraps instead of squeezing the heading to nothing beside the pill and the attribution', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const header = screen.getByTestId('approval-header')
    expect(classes(header)).toContain('flex-wrap')
    // The heading keeps a readable width: below it, it drops under the pill.
    const heading = screen.getByTestId('approval-title').parentElement!
    expect(classes(heading)).toContain('min-w-[9rem]')
    expect(classes(heading)).not.toContain('min-w-0')
  })

  it('wraps a long server name inside its chip and a long family name inside its Trust row', () => {
    const state = stateWithApproval()
    Object.assign(state.chat!.messages[1].meta!, { is_shell: '', tool_kind: 'other', tool_name: 'create_or_update_release_notes', mcp_server: 'repository_automation_and_release_management', full_command: undefined, base_command: undefined })
    const { unmount } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const kind = screen.getByTestId('approval-kind')
    expect(classes(kind)).toContain('max-w-full')
    expect(kind.querySelector('span')!.className).toContain(COMMAND_WRAP)
    unmount()
    const shell = stateWithApproval()
    Object.assign(shell.chat!.messages[1].meta!, { full_command: '/work/bin/repository_automation_and_release_management --help', base_command: '/work/bin/repository_automation_and_release_management' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(shell) })
    openTrust()
    expect(screen.getByTestId('trust-base-scope').className).toContain(COMMAND_WRAP)
  })

  it('bounds every text that does not wrap: the status pill and the command key', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const pill = screen.getByTestId('approval-header').firstElementChild as HTMLElement
    expect(classes(pill)).toContain('max-w-full')
    expect(classes(pill)).not.toContain('whitespace-nowrap')
    const key = screen.getByTestId('approval-command').previousElementSibling as HTMLElement
    expect(classes(key)).toContain('max-w-[40%]')
  })

  it('uses no break-all, and no unbounded nowrap, in the card, its menus and its receipt', () => {
    // The class audit as a gate: break-all splits every word at the edge.
    for (const file of ['components/chat-input/ToolApprovalCard.tsx', 'components/TrustDropdown.tsx', 'components/chat-input/CuedScrollBox.tsx', 'components/chat-input/approval.ts']) {
      const code = readFileSync(resolve(process.cwd(), 'src', file), 'utf8').split('\n').filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n')
      expect(code.match(/['"` ]break-all['"` ]/g) ?? [], file).toEqual([])
      expect(code.match(/whitespace-nowrap/g) ?? [], file).toEqual([])
    }
  })

  it('keeps who raised the request inside the card, wrapping a long name', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const chip = screen.getByTestId('raised-by')
    expect(chip).toHaveTextContent('Alexandra Montgomery-Fairweather')
    expect(classes(chip)).toContain('max-w-full')
    expect(classes(chip)).toContain('break-words')
    expect(classes(chip)).not.toContain('shrink-0')
  })
})
