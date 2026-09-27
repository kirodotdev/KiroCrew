/**
 * The composer sits on a Liquid Glass pane: `--glass-tint` over the blurred
 * transcript, a 1px lit rim instead of the wrapper's own border, and the
 * composer halo for depth. The wrapper's surface therefore goes transparent so
 * the pane shows through -- except while an approval box is fused to its top,
 * when it returns to the solid surface (the pane has rounded top corners and
 * the fused wrapper does not, so a transparent wrapper would show the transcript
 * through two notches). The pane stays mounted in both states: toggling it
 * would remount the editor and drop the draft's focus when an approval lands.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { createTestStore, renderWithProviders } from './helpers'
import type { RootState } from '../store'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

describe('composer liquid glass', () => {
  it('keeps the wrapper transparent so the glass pane shows through', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-transparent')
    expect(wrapper.className).toContain('border-[color:var(--glass-edge)]')
    expect(wrapper.className).not.toContain('bg-bg-elevated')
  })

  // With an approval box fused above, the wrapper goes back to a solid surface
  // and its plain border — and keeps the focus-within accent brightening, which
  // is the composer's only focus cue in that state (the halo is off while an
  // approval is attached and the textarea has no outline of its own).
  it('returns to a solid surface with the focus cue intact while an approval is attached', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'slot-1',
        messages: [
          { role: 'user', content: 'list files' },
          {
            role: 'permission',
            content: 'Running: ls /tmp',
            meta: { approval_id: 'ap-1', request_id: 'req-1', tool_input: '{"command":"ls /tmp"}', tool_title: 'Running: ls /tmp', tool_call_id: 'tc-1' },
          },
        ],
        toolLog: [],
        slotStatusDetail: {},
      } as unknown as RootState['chat'],
      dashboard: {
        slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false }],
        approvalMode: 'normal',
        connected: true,
        channelTrusted: false,
        refreshTrigger: 0,
        unreadSlots: [],
        updateProgress: null,
      } as unknown as RootState['dashboard'],
    })
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />, { store })
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-bg-elevated')
    expect(wrapper.className).toContain('border-border')
    expect(wrapper.className).toContain('focus-within:border-accent/50')
    expect(wrapper.className).not.toContain('bg-transparent')
  })

  it('mounts the glass pane around the wrapper, tinted from the theme token', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    // LiquidGlass renders [root > content div > children]; the root carries the
    // corner radius and the tint layer sits among its effect layers.
    const root = wrapper.parentElement?.parentElement as HTMLElement
    expect(root.style.borderRadius).toBe('16px')
    const layers = Array.from(root.querySelectorAll<HTMLElement>('div[aria-hidden="true"]'))
    expect(layers.some(l => l.style.background.includes('var(--glass-tint)'))).toBe(true)
  })

  it('defines --glass-tint and --glass-edge for both polarities', () => {
    expect(INDEX_CSS).toMatch(/:root \{ --glass-tint: rgba\(30, 30, 34, 0\.55\); --glass-edge: rgba\(255, 255, 255, 0\.14\); \}/)
    expect(INDEX_CSS).toMatch(/\[data-mode="light"\] \{ --glass-tint: rgba\(255, 255, 255, 0\.72\); --glass-edge: rgba\(0, 0, 0, 0\.10\); \}/)
  })

  // The pane must solidify wherever the app's other glass does: reduced
  // transparency, increased contrast, and a Chromium built without
  // backdrop-filter (#1817) — otherwise the transcript would show through the
  // box the user is typing into.
  it('solidifies under every glass fallback rule', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const root = screen.getByTestId('input-wrapper').parentElement?.parentElement as HTMLElement
    expect(root.classList.contains('liquid-glass')).toBe(true)
    for (const block of [/@supports not \(\(backdrop-filter[\s\S]*?\n\}/, /@media \(prefers-reduced-transparency: reduce\)\{[\s\S]*?\n\}/, /@media \(prefers-contrast: more\)\{[\s\S]*?\n\}/]) {
      const rule = INDEX_CSS.match(block)?.[0] ?? ''
      expect(rule, String(block)).toContain('.liquid-glass{ background:var(--bg-elevated) !important')
      expect(rule, String(block)).toContain('.liquid-glass>[aria-hidden="true"]{ display:none !important }')
    }
  })
})
