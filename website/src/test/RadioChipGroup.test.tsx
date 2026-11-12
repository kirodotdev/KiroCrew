/**
 * `RadioChipGroup` — the shared one-of-N chip group (#27459).
 *
 * A row of buttons that picks ONE value out of a set used to carry
 * `aria-pressed`, which a screen reader announces as N independent on/off
 * toggles: the user hears "pressed" / "not pressed" per button and is never told
 * the buttons are one single-select set. Each option is now a `role="radio"`
 * with `aria-checked` inside a named `role="radiogroup"`, with one Tab stop and
 * arrow-key movement (focus only by default, so passing over an option never
 * saves it).
 *
 * The behaviour block proves the primitive. The surface block is the revert
 * guard for every call site converted in #27459: putting `aria-pressed` back on
 * one of them (or dropping the shared group) turns it red.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useState, type KeyboardEvent } from 'react'
import { join } from 'node:path'

import { RadioChipGroup, radioChip } from '../components/RadioChipGroup'
import { readSource } from './readSource'

function Picker({ initial, onPick, selectOnArrow, disabled }: {
  initial: string | null
  onPick?: (v: string) => void
  selectOnArrow?: boolean
  disabled?: string
}) {
  const [value, setValue] = useState<string | null>(initial)
  return (
    <RadioChipGroup ariaLabel="Size" selectOnArrow={selectOnArrow}>
      {['S', 'M', 'L'].map(v => (
        <button key={v} type="button" disabled={v === disabled} {...radioChip(value === v)}
          onClick={() => { setValue(v); onPick?.(v) }}>{v}</button>
      ))}
    </RadioChipGroup>
  )
}

const radio = (name: string) => screen.getByRole('radio', { name })

describe('RadioChipGroup — behaviour', () => {
  it('is one named radio group whose options carry aria-checked, never aria-pressed', () => {
    render(<Picker initial="M" />)
    const group = screen.getByRole('radiogroup', { name: 'Size' })
    expect(group).toBeInTheDocument()
    expect(radio('M')).toHaveAttribute('aria-checked', 'true')
    expect(radio('S')).toHaveAttribute('aria-checked', 'false')
    for (const r of screen.getAllByRole('radio')) expect(r.hasAttribute('aria-pressed')).toBe(false)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('puts only the checked option in the Tab order', () => {
    render(<Picker initial="L" />)
    expect(radio('L').tabIndex).toBe(0)
    expect(radio('S').tabIndex).toBe(-1)
    expect(radio('M').tabIndex).toBe(-1)
  })

  it('makes the first enabled option the Tab stop when none is checked', () => {
    render(<Picker initial={null} disabled="S" />)
    expect(radio('M').tabIndex).toBe(0)
    expect(radio('S').tabIndex).toBe(-1)
    expect(radio('L').tabIndex).toBe(-1)
  })

  it('moves the Tab stop with the selection', () => {
    render(<Picker initial="S" />)
    fireEvent.click(radio('L'))
    expect(radio('L').tabIndex).toBe(0)
    expect(radio('S').tabIndex).toBe(-1)
  })

  it('arrow keys move focus only, wrapping at the ends; Home/End jump; Space/Enter (click) picks', () => {
    const onPick = vi.fn()
    render(<Picker initial="S" onPick={onPick} />)
    radio('S').focus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(radio('M')).toHaveFocus()
    fireEvent.keyDown(radio('M'), { key: 'ArrowDown' })
    fireEvent.keyDown(radio('L'), { key: 'ArrowRight' })
    expect(radio('S')).toHaveFocus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowLeft' })
    expect(radio('L')).toHaveFocus()
    fireEvent.keyDown(radio('L'), { key: 'Home' })
    expect(radio('S')).toHaveFocus()
    fireEvent.keyDown(radio('S'), { key: 'End' })
    expect(radio('L')).toHaveFocus()
    // Passing over an option never picks it: many consumers save on change.
    expect(onPick).not.toHaveBeenCalled()
    expect(radio('S')).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(radio('L'))
    expect(onPick).toHaveBeenCalledWith('L')
    expect(radio('L')).toHaveAttribute('aria-checked', 'true')
  })

  it('the Tab stop follows focus inside the group and returns to the checked option when focus leaves', () => {
    render(<><Picker initial="S" /><button type="button">after</button></>)
    radio('S').focus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(radio('M')).toHaveFocus()
    // Tab from M must leave the group, not step back to the checked S.
    expect(radio('M').tabIndex).toBe(0)
    expect(radio('S').tabIndex).toBe(-1)
    screen.getByRole('button', { name: 'after' }).focus()
    expect(radio('S').tabIndex).toBe(0)
    expect(radio('M').tabIndex).toBe(-1)
  })

  it('skips a disabled option', () => {
    render(<Picker initial="S" disabled="M" />)
    radio('S').focus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(radio('L')).toHaveFocus()
  })

  it('with selectOnArrow, arrows move focus AND select (the sidebar duration chips)', () => {
    const onPick = vi.fn()
    render(<Picker initial="S" onPick={onPick} selectOnArrow />)
    radio('S').focus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(radio('M')).toHaveFocus()
    expect(radio('M')).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(radio('M'), { key: 'End' })
    expect(radio('L')).toHaveAttribute('aria-checked', 'true')
    expect(onPick.mock.calls.map(c => c[0])).toEqual(['M', 'L'])
  })

  it('consumes the arrow key so a host menu does not also navigate', () => {
    const outer = vi.fn()
    render(
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- test harness listener only
      <div onKeyDown={outer}><Picker initial="S" /></div>,
    )
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(outer).not.toHaveBeenCalled()
    fireEvent.keyDown(radio('M'), { key: 'a' })
    expect(outer).toHaveBeenCalledTimes(1)
  })

  it("runs the caller's own key handler first and stands down when it prevents default", () => {
    const own = vi.fn((e: KeyboardEvent) => { if (e.key === 'ArrowRight') e.preventDefault() })
    function Host() {
      const [v, setV] = useState('S')
      return (
        <RadioChipGroup ariaLabel="Size" onKeyDown={own}>
          {['S', 'M'].map(x => <button key={x} type="button" {...radioChip(v === x)} onClick={() => setV(x)}>{x}</button>)}
        </RadioChipGroup>
      )
    }
    render(<Host />)
    radio('S').focus()
    fireEvent.keyDown(radio('S'), { key: 'ArrowRight' })
    expect(own).toHaveBeenCalled()
    expect(radio('S')).toHaveFocus()
    expect(radio('S')).toHaveAttribute('aria-checked', 'true')
  })
})

/**
 * Every one-of-N site #27459 converted. Each must draw its options through the
 * shared group and must not set `aria-pressed` from an equality test any more
 * (the one-of-N shape the issue grepped for). Genuine two-state toggles keep
 * `aria-pressed` and are not listed.
 */
const CONVERTED = [
  'apps/aws-control/DrivePageFileDialogs.tsx',
  'apps/aws-control/DrivePageLibrary.tsx',
  'apps/crew-companion/PanelViews.tsx',
  'apps/crew-companion/SettingsSection.tsx',
  'apps/issue-radar/components/CrewEditor.tsx',
  'apps/spec-builder/components/NewSpecView.tsx',
  'apps/workflows/WorkflowsRuns.tsx',
  'components/FileRenderers.tsx',
  'components/FolderConfigModal.tsx',
  'components/OnboardingFlow.tsx',
  'components/TagManagerList.tsx',
  'components/appstore/CategoryRail.tsx',
  'components/settings.tsx',
  'guide/GuideLayer.tsx',
  'pages/ProjectsPage.tsx',
  'pages/SessionsPage.tsx',
  'pages/WorldsPage.tsx',
  'pages/WorldsPopout.tsx',
  'pages/chat-sidebar/BroadcastPanel.tsx',
  'pages/chat-sidebar/SearchDock.tsx',
  'pages/chat-sidebar/sessionCleanup.tsx',
  'pages/chat/SessionColorPicker.tsx',
  'pages/chat/command-center/UltraCrewPanel.tsx',
  'pages/knowledge/SourcesList.tsx',
  'pages/overview/VectorMemoryCard.tsx',
  'pages/overview/WakaTimeTab.tsx',
  'pages/settings/DisplayPanel.tsx',
  'pages/system/PerformanceTab.tsx',
]

describe('RadioChipGroup — converted surfaces (revert guard)', () => {
  it.each(CONVERTED)('%s draws its one-of-N choice as a radio group', rel => {
    const src = readSource(join(__dirname, '..', rel))
    expect(src).toMatch(/<RadioChipGroup\b/)
    expect(src).toMatch(/\{\.\.\.radioChip\(/)
    expect(src).not.toMatch(/aria-pressed=\{[a-zA-Z.]+ === /)
  })
})
