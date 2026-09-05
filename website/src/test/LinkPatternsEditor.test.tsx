import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { LinkPatternsEditor, type LinkPatternRule } from '../pages/settings/ChatPanel'

// The rules editor keeps LOCAL rows and adopts the server value only when it
// moves somewhere genuinely new. Two movements must NOT replace local rows:
// our own save echoing back (the optimistic overlay masks the submitted
// value), and — the data-loss case — a REJECTED save's rollback, where the
// overlay reverts to the pre-save value after a 400 (e.g. duplicate
// patterns). Before the watermarks, that rollback re-ran the adopt effect
// and silently erased everything typed since the last successful save.
//
// The mirror-image data-loss case is the CONFIRMED save: once the server
// accepts the submitted value, that value is the new base — a later external
// change back to the pre-save value must be adopted, not swallowed as a
// rollback (which would leave stale rows to overwrite the external change on
// the next blur). The settlement of the promise `onSave` returns is what
// tells the two cases apart.

const URL_A = 'https://a.example/{match}'
const URL_B = 'https://b.example/{match}'

function patternInputs(): HTMLInputElement[] {
  return screen.getAllByPlaceholderText(/\\bPROJ-\\d\+\\b/) as HTMLInputElement[]
}

function editorWith(rules: LinkPatternRule[], onSave: (next: LinkPatternRule[]) => void | Promise<unknown> = () => {}) {
  return (
    <LinkPatternsEditor label="Text link patterns" rules={rules} onSave={onSave} />
  )
}

describe('LinkPatternsEditor server-value adoption', () => {
  it('preserves edited rows when a rejected save rolls the server value back', () => {
    const before: LinkPatternRule[] = [{ pattern: 'AA-\\d+', url: URL_A }]
    const saved: LinkPatternRule[] = []
    const { rerender } = render(editorWith(before, next => saved.push(...next)))

    // Edit the row and blur: the editor submits the cleaned rows.
    const input = patternInputs()[0]
    fireEvent.change(input, { target: { value: 'BB-\\d+' } })
    fireEvent.blur(input)
    expect(saved.map(r => r.pattern)).toEqual(['BB-\\d+'])

    // Optimistic overlay echoes the submitted value ... then the PUT fails
    // (400) and the overlay rolls back to the pre-save value.
    rerender(editorWith([{ pattern: 'BB-\\d+', url: URL_A }]))
    rerender(editorWith(before))

    // The typed row survives the rollback instead of reverting to AA-\d+.
    expect(patternInputs()[0].value).toBe('BB-\\d+')
  })

  it('sends the pattern exactly as typed: edge whitespace is load-bearing regex text', () => {
    // `PROJ-\d+ ` (trailing space) matches different text than `PROJ-\d+`.
    // Trimming on save would silently broaden the operator's regex — the
    // editor trims only to detect blank rows, and the server preserves the
    // pattern verbatim too.
    const saves: LinkPatternRule[][] = []
    render(editorWith([{ pattern: 'AA-\\d+', url: URL_A }], next => { saves.push(next) }))
    const input = patternInputs()[0]
    fireEvent.change(input, { target: { value: 'AA-\\d+ ' } })
    fireEvent.blur(input)
    expect(saves).toHaveLength(1)
    expect(saves[0][0].pattern).toBe('AA-\\d+ ')
  })

  it('still adopts a genuine external change', () => {
    const before: LinkPatternRule[] = [{ pattern: 'AA-\\d+', url: URL_A }]
    const { rerender } = render(editorWith(before))
    rerender(editorWith([{ pattern: 'CC-\\d+', url: URL_B }]))
    expect(patternInputs()[0].value).toBe('CC-\\d+')
  })

  it('carries a typed-but-unsaved draft across an external adopt', () => {
    const before: LinkPatternRule[] = [{ pattern: 'AA-\\d+', url: URL_A }]
    const { rerender } = render(editorWith(before))

    // Start typing a new rule (add a row, fill the pattern only — a
    // half-edited draft the commit filter never persists).
    fireEvent.click(screen.getByRole('button', { name: /add pattern/i }))
    const draft = patternInputs()[1]
    fireEvent.change(draft, { target: { value: 'DR-\\d+' } })

    // Another client changes the config while the draft is in progress.
    rerender(editorWith([{ pattern: 'CC-\\d+', url: URL_B }]))

    // The external change is adopted AND the draft survives beside it.
    const values = patternInputs().map(i => i.value)
    expect(values).toContain('CC-\\d+')
    expect(values).toContain('DR-\\d+')
  })

  it('does not resurrect a clean row the external change deleted', () => {
    const before: LinkPatternRule[] = [
      { pattern: 'AA-\\d+', url: URL_A },
      { pattern: 'CC-\\d+', url: URL_B },
    ]
    const { rerender } = render(editorWith(before))
    // External change deletes the second rule; nothing was typed locally,
    // so it is in the adopted baseline and must NOT be kept as a draft.
    rerender(editorWith([{ pattern: 'AA-\\d+', url: URL_A }]))
    expect(patternInputs().map(i => i.value)).toEqual(['AA-\\d+'])
  })

  it('flags the half-filled row that blocks every other edit from saving', () => {
    const saves: LinkPatternRule[][] = []
    render(editorWith([{ pattern: 'AA-\\d+', url: URL_A }], next => { saves.push(next) }))

    // Add a row and fill only the pattern: this row now blocks the commit.
    fireEvent.click(screen.getByRole('button', { name: /add pattern/i }))
    const draft = patternInputs()[1]
    fireEvent.change(draft, { target: { value: 'DR-\\d+' } })
    fireEvent.blur(draft)

    // The blocked state is visible on the offending row, not silent.
    expect(screen.getByText(/Incomplete row/)).toBeTruthy()
    // And the commit really was withheld while the row is half-filled.
    expect(saves).toEqual([])
  })

  it('flags URL templates registration refuses, not just missing scheme or token', () => {
    // The inline check is the registry's own acceptance rule
    // (`configUrlTemplateOk`), so the two classes a bare `^https?://` regex
    // waves through — userinfo, and a `{match}` steering the authority — flag
    // where they are typed AND are withheld from the commit, instead of
    // saving-then-never-linkifying (or reaching the server's 400).
    const saves: LinkPatternRule[][] = []
    render(editorWith([{ pattern: 'AA-\\d+', url: URL_A }], next => { saves.push(next) }))
    const url = screen.getAllByDisplayValue(URL_A)[0] as HTMLInputElement

    for (const bad of ['https://u:p@host.example/{match}', 'https://{match}.example.com/x']) {
      fireEvent.change(url, { target: { value: bad } })
      fireEvent.blur(url)
      expect(screen.getByText(/Must be an absolute http/)).toBeTruthy()
      expect(saves).toEqual([]) // withheld, not sent to fail server-side
    }

    // A well-formed template clears the flag and commits.
    fireEvent.change(url, { target: { value: URL_B } })
    fireEvent.blur(url)
    expect(screen.queryByText(/Must be an absolute http/)).toBeNull()
    expect(saves.length).toBe(1)
    expect(saves[0][0].url).toBe(URL_B)
  })

  it('adopts an external restore of the pre-save value after a CONFIRMED save', async () => {
    const before: LinkPatternRule[] = [{ pattern: 'AA-\\d+', url: URL_A }]
    let confirm!: () => void
    const settled = new Promise<void>(res => { confirm = res })
    const { rerender } = render(editorWith(before, () => settled))

    const input = patternInputs()[0]
    fireEvent.change(input, { target: { value: 'BB-\\d+' } })
    fireEvent.blur(input)

    // Optimistic echo of the submitted value, then the server confirms it.
    rerender(editorWith([{ pattern: 'BB-\\d+', url: URL_A }]))
    await act(async () => {
      confirm()
      await settled
    })

    // An external client (another tab, `kirocrew config set`) restores the
    // pre-save value. Without the settlement-advanced watermark this looked
    // like a failed save's rollback and was swallowed — leaving stale rows
    // to overwrite the external change on the next blur.
    rerender(editorWith(before))
    expect(patternInputs()[0].value).toBe('AA-\\d+')
  })

  it('preserves edited rows when the save promise REJECTS and the overlay rolls back', async () => {
    const before: LinkPatternRule[] = [{ pattern: 'AA-\\d+', url: URL_A }]
    let reject!: (e: unknown) => void
    const settled = new Promise<void>((_res, rej) => { reject = rej })
    const { rerender } = render(editorWith(before, () => settled))

    const input = patternInputs()[0]
    fireEvent.change(input, { target: { value: 'BB-\\d+' } })
    fireEvent.blur(input)

    // Optimistic echo ... then the PUT is rejected and the overlay rolls
    // back to the pre-save value.
    rerender(editorWith([{ pattern: 'BB-\\d+', url: URL_A }]))
    await act(async () => {
      reject(new Error('400 invalid_link_patterns'))
      await settled.catch(() => undefined)
    })
    rerender(editorWith(before))

    // The typed row survives: rejection must clear only the echo mark, never
    // advance the adopted watermark past the rollback target.
    expect(patternInputs()[0].value).toBe('BB-\\d+')
  })
})
