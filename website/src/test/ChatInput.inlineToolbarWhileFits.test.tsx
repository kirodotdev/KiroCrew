import { describe, it, expect, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * `inlineToolbarWhileFits` (#19608): the Crewmates DM composer is ONE row --
 * attach, text, mic, send on a single line -- while the draft fits that line,
 * and the ordinary two-row shape (toolbar under the text) once it does not or
 * once something other than text is staged. The shape is pinned on the inner
 * box as `data-inline-toolbar`, which the capture harness and these tests read.
 *
 * happy-dom lays nothing out: the fit probe measures a zero height against a
 * zero line height, so any draft WITHOUT a newline "fits". That is enough to
 * pin the rules that do not depend on layout -- the empty box, a newline, a
 * staged file, a quote, the default-off host -- while the wrap boundary itself
 * is the capture harness's (`capture-crew-composer-one-row.mjs`).
 */

const wrapper = () => screen.getByTestId('input-wrapper')
const inline = () => wrapper().getAttribute('data-inline-toolbar') === 'true'

describe('ChatInput inlineToolbarWhileFits: the one-row shape', () => {
  it('is one row with an empty draft: the box is a row, the buttons are flex items of it', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits onUploadFiles={vi.fn()} />)
    expect(inline()).toBe(true)
    expect(wrapper().className).toContain('flex items-center')
    // A finite pill radius (half the 46px box), not `rounded-full`: the
    // corner then eases to the two-row 16px instead of holding a 9999px
    // radius until the last frame of the transition.
    expect(wrapper().className).toContain('rounded-[23px]')
    expect(wrapper().className).not.toContain('rounded-full')
    // The bottom row dissolves (`contents`) so its two clusters sit on the
    // text's line, the attach cluster first and the send cluster last.
    const row = screen.getByTestId('composer-bottom-row')
    expect(row.className).toBe('contents')
    expect(row.firstElementChild!.className).toContain('order-first')
    expect(row.lastElementChild!.className).toContain('order-last')
    // The same three controls as the two-row shape: attach, and send (disabled
    // on an empty draft, as always).
    expect(screen.getByRole('button', { name: /attach files|add files/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('stays one row on a short draft, with Send live on the single line', () => {
    renderWithProviders(<ChatInput value="ship it" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    expect(inline()).toBe(true)
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('opens to two rows on a newline: a second line never fits one row, whatever the width', () => {
    renderWithProviders(<ChatInput value={'first\nsecond'} onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    expect(inline()).toBe(false)
    expect(wrapper().className).toContain('rounded-2xl')
    expect(screen.getByTestId('composer-bottom-row').className).toContain('justify-between')
  })

  it('keeps one row through focus and blur on a list draft, with the list-indent hint screen-reader only', () => {
    renderWithProviders(<ChatInput value="- item" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    const ta = screen.getByRole('textbox')
    expect(inline()).toBe(true)
    expect(screen.getByTestId('list-indent-hint').className).toBe('sr-only')
    fireEvent.focus(ta)
    // Focus does not reshape the box: the hint stays out of the visual row
    // and is still described to screen readers.
    expect(inline()).toBe(true)
    expect(screen.getByTestId('list-indent-hint').className).toBe('sr-only')
    expect(ta.getAttribute('aria-describedby')).toContain(screen.getByTestId('list-indent-hint').id)
    fireEvent.blur(ta)
    expect(inline()).toBe(true)
  })

  it('shows the list-indent hint under the text once the draft no longer fits one row', () => {
    renderWithProviders(<ChatInput value={'- item\n- next'} onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    const ta = screen.getByRole('textbox')
    fireEvent.focus(ta)
    expect(inline()).toBe(false)
    expect(screen.getByTestId('list-indent-hint').className).toContain('text-[12px]')
  })

  it('opens to two rows while a file is staged: the file strip has no place on one line', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits pendingFiles={['a.txt']} onRemoveFile={vi.fn()} />)
    expect(inline()).toBe(false)
  })

  // A staged folder (`@src/` in the draft) draws the same strip as a file.
  it('opens to two rows while a folder is staged', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits pendingDirs={['src/']} onRemoveDir={vi.fn()} />)
    expect(inline()).toBe(false)
  })

  // An upload in flight draws the send-held line under a draft; on the single
  // row it would sit beside the text and squeeze it.
  it('opens to two rows while a draft waits on an upload', () => {
    renderWithProviders(<ChatInput value="see attached" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits holdSend uploading onUploadFiles={vi.fn()} />)
    expect(screen.getByTestId('composer-send-held')).toBeInTheDocument()
    expect(inline()).toBe(false)
  })

  it('opens to two rows while a quote is staged', () => {
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits pendingQuote={{ text: 'quoted', role: 'assistant' }} onRemoveQuote={vi.fn()} />,
    )
    expect(inline()).toBe(false)
  })

  it('mounts the fit probe only for an opted-in host, hidden and out of the accessibility tree', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    const probe = screen.getByTestId('composer-one-row-probe')
    expect(probe.getAttribute('aria-hidden')).toBe('true')
    expect(probe.className).toContain('invisible')
    expect(probe.className).toContain('pointer-events-none')
    // The touch font floor (16px, keyed on this attribute) must reach the
    // probe too, or it measures at 14px while the textarea wraps at 16px.
    expect(probe.hasAttribute('data-composer-typo')).toBe(true)
  })

  // One row centres the text line on the buttons: equal padding above and
  // below, set on the text column so the textarea, its paste mirror and the
  // Lexical editor all read it. Two rows keep INPUT_TYPO's 12px over 4px.
  it('centres the text line on the single row, and only there', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    const column = () => screen.getByRole('textbox').parentElement!
    expect(column().className).toContain('[--composer-pt:max(0px,calc((44px_-_1.5em)_/_2))]')
    expect(column().className).toContain('[--composer-pb:max(0px,calc((44px_-_1.5em)_/_2))]')
    expect(screen.getByRole('textbox').className).toContain('pt-[var(--composer-pt,0.75rem)]')
  })

  it('keeps the two-row text padding once the box opens', () => {
    renderWithProviders(<ChatInput value={'first\nsecond'} onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    expect(screen.getByRole('textbox').parentElement!.className).not.toContain('--composer-pt')
  })

  // The ordinary composer draws session chrome in its toolbar and keeps two
  // rows: a host that does not opt in is untouched -- no shape attribute, no
  // probe, the bottom row as it was.
  it('is off by default: the ordinary composer keeps its two rows and mounts no probe', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    expect(wrapper().hasAttribute('data-inline-toolbar')).toBe(false)
    expect(wrapper().className).toContain('rounded-2xl')
    expect(wrapper().className).not.toContain('rounded-full')
    expect(screen.queryByTestId('composer-one-row-probe')).toBeNull()
    expect(screen.getByTestId('composer-bottom-row').className).toContain('justify-between')
    // No sizer either: the box's parent is the same element it was before.
    expect(screen.queryByTestId('composer-sizer')).toBeNull()
    expect(wrapper().className).not.toContain('shrink-0')
  })

  // The sizer around the box is what animates the change of shape; it wears
  // the same corner the box does so the clip during the slide is rounded too.
  it('wraps the box in a bottom-aligned sizer that shares its corner', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} inlineToolbarWhileFits />)
    const sizer = screen.getByTestId('composer-sizer')
    expect(sizer.contains(wrapper())).toBe(true)
    expect(sizer.className).toContain('justify-end')
    expect(sizer.className).toContain('rounded-[23px]')
    expect(sizer.className).toContain('overflow-hidden')
    // The inner box must not shrink to a pinned sizer, or its measure never moves.
    expect(wrapper().className).toContain('shrink-0')
  })
})
