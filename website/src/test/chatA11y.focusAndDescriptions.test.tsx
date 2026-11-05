/**
 * Chat surface accessibility: three controls a keyboard or screen-reader user
 * could not use.
 *
 * - The jump-to-latest pill unmounts once the transcript reaches the bottom, so
 *   the person who pressed it from the keyboard was left on <body>. Focus moves
 *   to the nearest composer instead, never to a queued-message editor, and
 *   never after a tap or click.
 * - An elicitation card shows each field's description, but every control
 *   carries its own `aria-label`, so the description was never read with it.
 *   The control now points at it with `aria-describedby`.
 * - An inactive side-panel tab chip's close button, and an artifact tag's
 *   remove button, are hidden until hover. Keyboard focus in the chip now
 *   reveals them too, as the session and embed tab strips do. jsdom does not
 *   compute Tailwind's opacity, so that half pins the class contract on each
 *   site's source. The docked terminal tab is left as it is:
 *   `BottomTerminalPanel.rename.test.tsx` pins its close as hidden on focus.
 */
import React, { useState } from 'react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { JumpToBottomButton } from '../app-sdk/ChatScrollChrome'
import ElicitationCard, { type ElicitationField } from '../pages/chat/ElicitationCard'
import type { ChatMessage } from '../types'

afterEach(cleanup)

describe('jump-to-latest pill hands focus off when it leaves', () => {
  let setVisible: (v: boolean) => void = () => {}
  function Dock({ boxed = false }: { boxed?: boolean }) {
    const [visible, set] = useState(true)
    setVisible = set
    const pill = <JumpToBottomButton visible={visible} onClick={() => set(false)} />
    return (
      <div className="relative" data-testid="dock">
        {boxed ? <div className="relative">{pill}</div> : pill}
        {/* A queued-message editor sits above the composer in the dock and
            carries no composer marker: it must never receive the hand-off. */}
        <textarea aria-label="Queued message" />
        <textarea aria-label="Message" {...(boxed ? { 'data-composer-editor': '' } : { 'data-composer-input': '' })} />
      </div>
    )
  }

  /** A dock whose composer is collapsed: the pill and no composer field. */
  function SideFreeDock() {
    const [visible, set] = useState(true)
    setVisible = set
    return <JumpToBottomButton visible={visible} onClick={() => set(false)} />
  }

  it.each([
    ['beside the composer', false],
    ['in its own box, as the embed mounts it', true],
  ])('moves focus to the composer when the pressed pill unmounts (%s)', (_label, boxed) => {
    render(<Dock boxed={boxed} />)
    const pill = screen.getByTestId('jump-to-bottom')
    pill.focus()
    expect(document.activeElement).toBe(pill)

    act(() => setVisible(false))

    expect(screen.queryByTestId('jump-to-bottom')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Message' }))
  })

  it('moves nobody after a tap or click: the composer would raise the on-screen keyboard', () => {
    render(<Dock />)
    const pill = screen.getByTestId('jump-to-bottom')
    fireEvent.pointerDown(pill)
    pill.focus()

    act(() => setVisible(false))

    expect(document.activeElement).not.toBe(screen.getByRole('textbox', { name: 'Message' }))
  })

  it('still hands off on a key press after a cancelled pointer press', () => {
    render(<Dock />)
    const pill = screen.getByTestId('jump-to-bottom')
    fireEvent.pointerDown(pill)
    pill.focus()
    fireEvent.keyDown(pill, { key: 'Enter' })

    act(() => setVisible(false))

    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Message' }))
  })

  it('never lands in the side chat when the main composer is collapsed', () => {
    render(
      <div>
        {/* The main page: no pane edge, so only the side-chat skip holds. */}
        <div className="relative"><SideFreeDock /></div>
        <div data-side-chat-input="">
          <textarea aria-label="Side message" data-composer-input="" />
        </div>
      </div>,
    )
    const pill = screen.getByTestId('jump-to-bottom')
    pill.focus()

    act(() => setVisible(false))

    expect(document.activeElement).not.toBe(screen.getByRole('textbox', { name: 'Side message' }))
  })

  it('moves nobody when the pill leaves without focus', () => {
    render(<Dock />)
    const other = document.createElement('button')
    document.body.appendChild(other)
    other.focus()

    act(() => setVisible(false))

    expect(document.activeElement).toBe(other)
    other.remove()
  })
})

describe('elicitation field descriptions are read with their control', () => {
  const FIELDS: ElicitationField[] = [
    { name: 'confirm', kind: 'boolean', required: true, title: 'Confirm', description: 'Deletes the file for good' },
    { name: 'mode', kind: 'enum', required: false, title: 'Mode', description: 'How hard to try', options: [{ value: 'fast', label: 'Fast' }, { value: 'safe', label: 'Safe' }] },
    { name: 'reason', kind: 'string', required: false, title: 'Reason', description: 'Shown in the audit log' },
    { name: 'note', kind: 'string', required: false, title: 'Note' },
  ]
  const card: ChatMessage = {
    role: 'elicitation',
    content: 'Really delete probe.txt?',
    meta: { approval_id: 'rid-1', mid: 'mid-1', mode: 'form', message: 'Really delete probe.txt?', fields: FIELDS },
  } as ChatMessage

  it.each([
    ['checkbox', 'Confirm', 'Deletes the file for good'],
    ['combobox', 'Mode', 'How hard to try'],
    ['textbox', 'Reason', 'Shown in the audit log'],
  ] as const)('the %s %s is described by its description', (role, name, description) => {
    render(<ElicitationCard message={card} slot="dashboard:main" />)
    expect(screen.getByRole(role, { name })).toHaveAccessibleDescription(description)
  })

  it('a field with no description gets no aria-describedby', () => {
    render(<ElicitationCard message={card} slot="dashboard:main" />)
    expect(screen.getByRole('textbox', { name: 'Note' })).not.toHaveAttribute('aria-describedby')
  })
})

describe('inactive tab chip close buttons are revealed on keyboard focus', () => {
  const SRC = join(__dirname, '..')
  // Each site's hidden-until-hover close or remove button: the class run that
  // hides it at rest must also carry the focus-within reveal of its group. An
  // `aria-hidden` glyph (the rename pencil inside the focusable title) is
  // decoration, not a control a keyboard user has to reach.
  it.each([
    ['pages/chat/SidePanelTabChip.tsx', 'group-hover:opacity-70', 'group-focus-within:opacity-70'],
    ['pages/ArtifactDetailPageHeader.tsx', 'group-hover:opacity-100', 'group-focus-within:opacity-100'],
  ])('%s', (file, hoverReveal, focusReveal) => {
    const source = readFileSync(join(SRC, file), 'utf8')
    const runs = source.split('\n').filter(line =>
      line.includes('opacity-0') && line.includes(hoverReveal) && !line.includes('aria-hidden'))
    expect(runs.length).toBeGreaterThan(0)
    for (const run of runs) expect(run).toContain(focusReveal)
  })
})
