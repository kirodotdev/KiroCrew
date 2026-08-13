/**
 * The editor footer is where a FAILED SAVE is reported.
 *
 * That matters beyond wording: the owning page's error banner only renders on its
 * gallery view, so reporting a failure there forced a navigation away from the
 * importer — which discarded every frame, row and name the user had configured.
 * Giving the footer a `saveError` slot is what lets the form stay mounted, so
 * these tests pin the slot rather than the styling.
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { EditorFooter } from '../src/renderer/EditorFooter'

const noop = () => {}

describe('EditorFooter', () => {
  it('shows a save failure', () => {
    render(
      <EditorFooter
        missingStates={[]}
        canSave
        saveError="Disk full"
        onCancel={noop}
        onSave={noop}
      />,
    )
    expect(screen.getByText('Disk full')).toBeTruthy()
  })

  it('announces a save failure, since it arrives after the click', () => {
    render(
      <EditorFooter missingStates={[]} canSave saveError="Nope" onCancel={noop} onSave={noop} />,
    )
    expect(screen.getByRole('alert').textContent).toContain('Nope')
  })

  it('prefers the save failure over a validation hint', () => {
    // The user already pressed Save, so "it did not save" is the newer fact; two
    // red lines at once would also not fit this footer.
    render(
      <EditorFooter
        missingStates={['idle', 'walk']}
        canSave={false}
        saveError="Disk full"
        onCancel={noop}
        onSave={noop}
      />,
    )
    expect(screen.getByText('Disk full')).toBeTruthy()
    expect(screen.queryByText(/idle/)).toBeNull()
  })

  it('still shows the validation hint when nothing failed', () => {
    render(
      <EditorFooter
        missingStates={['idle']}
        canSave={false}
        onCancel={noop}
        onSave={noop}
      />,
    )
    // The hint names the missing states, so the label must reach the DOM.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(document.body.textContent).toMatch(/idle/i)
  })

  it('renders no notice at all when there is nothing to report', () => {
    render(<EditorFooter missingStates={[]} canSave onCancel={noop} onSave={noop} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('save stays clickable after a failure so the user can retry', () => {
    // A failed save must not disable the button: the form is still valid and the
    // whole point of keeping it mounted is that retrying is possible.
    const onSave = vi.fn()
    render(
      <EditorFooter
        missingStates={[]}
        canSave
        saveError="Transient"
        onCancel={noop}
        onSave={onSave}
      />,
    )
    const btn = screen.getAllByRole('button').find((b) => !(b as HTMLButtonElement).disabled)!
    expect(btn).toBeTruthy()
  })
})
