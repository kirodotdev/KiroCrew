// Editing a spec document from the app, and the compare-and-swap guard that makes
// it safe. Before this the documents were read-only, so every correction — a typo,
// a stale acceptance criterion — cost a full model turn.
//
// The guard under test is that the save carries the hash the editor OPENED with.
// DocView re-renders on every 2.5s detail poll, so reading the hash from the live
// `detail` prop at submit time would quietly re-base the edit onto the agent's
// newer version and defeat the conflict check entirely.
import { describe, it, expect, vi } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import DocView from '../apps/spec-builder/components/DocView'
import { SpecApiError, type SpecDetail } from '../apps/spec-builder/api'

const HASH_OLD = 'a'.repeat(64)
const HASH_NEW = 'b'.repeat(64)

function makeDetail(over: Partial<SpecDetail> = {}): SpecDetail {
  return {
    name: 'thing',
    working_dir: '/w',
    spec_dir: '/w/.kiro/specs/thing',
    phase: 'design',
    running: false,
    files: { 'requirements.md': '# the original text', 'design.md': null, 'tasks.md': null },
    docs: { 'requirements.md': { hash: HASH_OLD, editable: true } },
    state: null,
    context: {},
    ...over,
  } as unknown as SpecDetail
}

describe('DocView editing', () => {
  it('saves the edited text against the hash the editor opened with', async () => {
    const saveDoc = vi.fn().mockResolvedValue(undefined)
    render(<DocView detail={makeDetail()} tab="requirements" addComment={vi.fn()} saveDoc={saveDoc} />)

    act(() => { fireEvent.click(screen.getByRole('button', { name: /edit requirements/i })) })
    const box = screen.getByRole('textbox', { name: /edit requirements/i })
    expect(box).toHaveValue('# the original text')

    act(() => { fireEvent.change(box, { target: { value: '# edited by hand' } }) })
    act(() => { fireEvent.click(screen.getByRole('button', { name: /^save$/i })) })

    await waitFor(() => expect(saveDoc).toHaveBeenCalledTimes(1))
    expect(saveDoc).toHaveBeenCalledWith('requirements.md', '# edited by hand', HASH_OLD)
  })

  it('keeps the OPENED hash as the save base when a poll lands mid-edit', async () => {
    // The regression this exists for: the agent writes while the user is typing,
    // the 2.5s poll delivers a new hash, and a save based on THAT would overwrite
    // the agent's text while the server's check saw a matching base and allowed it.
    const saveDoc = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <DocView detail={makeDetail()} tab="requirements" addComment={vi.fn()} saveDoc={saveDoc} />,
    )
    act(() => { fireEvent.click(screen.getByRole('button', { name: /edit requirements/i })) })
    const box = screen.getByRole('textbox', { name: /edit requirements/i })
    act(() => { fireEvent.change(box, { target: { value: '# mine' } }) })

    // A poll arrives carrying the agent's newer version of the same document.
    rerender(
      <DocView
        detail={makeDetail({
          files: { 'requirements.md': '# the agent rewrote it', 'design.md': null, 'tasks.md': null },
          docs: { 'requirements.md': { hash: HASH_NEW, editable: true } },
        })}
        tab="requirements"
        addComment={vi.fn()}
        saveDoc={saveDoc}
      />,
    )
    act(() => { fireEvent.click(screen.getByRole('button', { name: /^save$/i })) })

    await waitFor(() => expect(saveDoc).toHaveBeenCalledTimes(1))
    expect(saveDoc.mock.calls[0][2]).toBe(HASH_OLD)
  })

  it('explains a conflict rather than reporting a generic failure', async () => {
    const saveDoc = vi.fn().mockRejectedValue(new SpecApiError('server prose', 'doc_conflict'))
    render(<DocView detail={makeDetail()} tab="requirements" addComment={vi.fn()} saveDoc={saveDoc} />)

    act(() => { fireEvent.click(screen.getByRole('button', { name: /edit requirements/i })) })
    act(() => { fireEvent.click(screen.getByRole('button', { name: /^save$/i })) })

    // Matched on the CODE, not the translated prose — the message differs per locale.
    await waitFor(() => expect(screen.getByText(/changed while you were editing/i)).toBeTruthy())
    // The draft survives: the user's text is the thing that must not be lost.
    expect(screen.getByRole('textbox', { name: /edit requirements/i })).toBeTruthy()
  })

  it('offers no editor for a document whose rendering is redacted', () => {
    // Saving the redacted rendering back would persist [redacted] over the real
    // value, so the app refuses to write it and says why.
    render(
      <DocView
        detail={makeDetail({ docs: { 'requirements.md': { hash: HASH_OLD, editable: false, reason: 'redacted' } } })}
        tab="requirements"
        addComment={vi.fn()}
        saveDoc={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: /edit requirements/i })).toBeNull()
    expect(screen.getByText(/redacted/i)).toBeTruthy()
  })

  it('offers no editor at all when the app passes no save handler', () => {
    render(<DocView detail={makeDetail()} tab="requirements" addComment={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /edit requirements/i })).toBeNull()
  })

  it('drops an unsaved draft when the user switches document', () => {
    // Carrying it would let a save write THIS document's text into another file.
    const saveDoc = vi.fn().mockResolvedValue(undefined)
    const detail = makeDetail({
      files: { 'requirements.md': '# reqs', 'design.md': '# design', 'tasks.md': null },
      docs: {
        'requirements.md': { hash: HASH_OLD, editable: true },
        'design.md': { hash: HASH_NEW, editable: true },
      },
    })
    const { rerender } = render(
      <DocView detail={detail} tab="requirements" addComment={vi.fn()} saveDoc={saveDoc} />,
    )
    act(() => { fireEvent.click(screen.getByRole('button', { name: /edit requirements/i })) })
    act(() => {
      fireEvent.change(screen.getByRole('textbox', { name: /edit requirements/i }), {
        target: { value: '# half-typed' },
      })
    })

    rerender(<DocView detail={detail} tab="design" addComment={vi.fn()} saveDoc={saveDoc} />)

    expect(screen.queryByRole('textbox', { name: /edit design/i })).toBeNull()
    expect(screen.getByRole('button', { name: /edit design/i })).toBeTruthy()
  })

  it('does not raise the review-comment pill over the editor', () => {
    // Selecting inside a textarea is ordinary text selection, not a review
    // gesture: offering to send the agent feedback about unsaved text is wrong.
    const saveDoc = vi.fn().mockResolvedValue(undefined)
    render(<DocView detail={makeDetail()} tab="requirements" addComment={vi.fn()} saveDoc={saveDoc} />)
    act(() => { fireEvent.click(screen.getByRole('button', { name: /edit requirements/i })) })

    const box = screen.getByRole('textbox', { name: /edit requirements/i })
    vi.spyOn(window, 'getSelection').mockReturnValue({
      toString: () => 'some selected words',
      rangeCount: 1,
      getRangeAt: () => ({
        commonAncestorContainer: box,
        getBoundingClientRect: () => ({ left: 10, top: 10, width: 40, height: 12 }),
      }),
    } as unknown as Selection)
    act(() => { fireEvent.mouseUp(box) })

    expect(screen.queryByRole('button', { name: /comment on the selected passage/i })).toBeNull()
    vi.restoreAllMocks()
  })
})
