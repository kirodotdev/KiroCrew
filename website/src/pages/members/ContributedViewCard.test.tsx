/**
 * Generic rendering of a contributed `<app>/<key>` view, and the store rules a
 * contributed row depends on.
 *
 * The load-bearing case is the per-key seq: a contributed row seeded at the
 * roster block's `asOfSeq` instead of its OWN seq would make higher-seq-wins
 * drop the contributor's next live push, and the card would freeze at its
 * baseline with nothing logged anywhere.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, beforeEach } from 'vitest'

import { memberProjectionStore } from '../../state/memberProjectionStore'
import type { ContributedView } from '../../state/memberProjectionTypes'
import { ContributedViewCard, ContributedViews, resolvePath, scalarText } from './ContributedViewCard'

const SLUG = 'code-reviewer'

function view(over: Partial<ContributedView> = {}): ContributedView {
  return { key: 'demoapp/count', value: { n: 3 }, seq: 1, ...over }
}

describe('scalarText', () => {
  it('stringifies scalars and JSON-encodes containers', () => {
    expect(scalarText('a')).toBe('a')
    expect(scalarText(3)).toBe('3')
    expect(scalarText(false)).toBe('false')
    expect(scalarText({ a: 1 })).toBe('{"a":1}')
    expect(scalarText(null)).toBe('')
  })

  it('truncates a long string so one value cannot fill the drawer', () => {
    const out = scalarText('x'.repeat(1000))
    expect(out.length).toBeLessThan(1000)
    expect(out.endsWith('…')).toBe(true)
  })
})

describe('resolvePath', () => {
  it('reads nested keys and array indices', () => {
    const value = { rows: [{ name: 'a' }, { name: 'b' }] }
    expect(resolvePath(value, 'rows.1.name')).toBe('b')
  })

  it('returns undefined for a miss rather than throwing', () => {
    expect(resolvePath({ a: 1 }, 'a.b.c')).toBeUndefined()
    expect(resolvePath(null, 'a')).toBeUndefined()
  })
})

describe('ContributedViewCard', () => {
  it('titles an untitled card by the key name segment, not the wire syntax', () => {
    render(<ContributedViewCard view={view()} />)
    // `demoapp/count` -> heading `count`; the app pill carries `demoapp`.
    expect(screen.getByText('count')).toBeTruthy()
    expect(screen.getByText('demoapp')).toBeTruthy()
    // The raw `<app>/<key>` wire syntax is not shown as the heading text.
    expect(screen.queryByText('demoapp/count')).toBeNull()
  })

  it('prefers a published title', () => {
    render(<ContributedViewCard view={view({ schema: { kind: 'badge', title: 'Pings' } })} />)
    expect(screen.getByText('Pings')).toBeTruthy()
  })

  it('renders a badge from a path selector', () => {
    render(
      <ContributedViewCard view={view({ schema: { kind: 'badge', path: ['n'] } })} />,
    )
    expect(screen.getByTestId('contributed-badge').textContent).toBe('3')
  })

  it('renders text', () => {
    render(
      <ContributedViewCard
        view={view({ value: { msg: 'all clear' }, schema: { kind: 'text', path: ['msg'] } })}
      />,
    )
    expect(screen.getByTestId('contributed-text').textContent).toBe('all clear')
  })

  it('renders a list from the first selector', () => {
    render(
      <ContributedViewCard
        view={view({ value: { items: ['a', 'b'] }, schema: { kind: 'list', path: ['items'] } })}
      />,
    )
    expect(screen.getByTestId('contributed-list').textContent).toBe('ab')
  })

  it('renders a table with the declared columns', () => {
    render(
      <ContributedViewCard
        view={view({
          value: { rows: [{ a: 1, b: 2, secret: 'x' }] },
          schema: { kind: 'table', path: ['rows', 'a', 'b'] },
        })}
      />,
    )
    const table = screen.getByTestId('contributed-table')
    expect(table.textContent).toContain('1')
    expect(table.textContent).toContain('2')
    // A field the schema did not name is not rendered.
    expect(table.textContent).not.toContain('x')
  })

  it('derives table columns from the rows when none are declared', () => {
    render(
      <ContributedViewCard
        view={view({ value: [{ a: 1 }, { a: 2 }], schema: { kind: 'table' } })}
      />,
    )
    expect(screen.getByTestId('contributed-table').textContent).toContain('a')
  })

  it('falls back to a key-value dump with no schema', () => {
    render(<ContributedViewCard view={view({ value: { open: 2, closed: 5 } })} />)
    const dump = screen.getByTestId('contributed-keyvalue')
    expect(dump.textContent).toContain('open')
    expect(dump.textContent).toContain('2')
  })

  it('says so rather than rendering an empty body for an empty list', () => {
    render(<ContributedViewCard view={view({ value: { items: [] }, schema: { kind: 'list', path: ['items'] } })} />)
    expect(screen.getByTestId('contributed-empty')).toBeTruthy()
  })

  it('marks a sliced list with "+N more" rather than reading as complete', () => {
    const items = Array.from({ length: 25 }, (_, i) => `row${i}`)
    render(<ContributedViewCard view={view({ value: { items }, schema: { kind: 'list', path: ['items'] } })} />)
    // MAX_ROWS is 20, so 5 rows are hidden.
    expect(screen.getByTestId('contributed-more').textContent).toBe('+5 more')
  })

  it('does not mark a list at or under the row cap', () => {
    const items = Array.from({ length: 20 }, (_, i) => `row${i}`)
    render(<ContributedViewCard view={view({ value: { items }, schema: { kind: 'list', path: ['items'] } })} />)
    expect(screen.queryByTestId('contributed-more')).toBeNull()
  })

  it('marks a sliced table with "+N more"', () => {
    const rows = Array.from({ length: 23 }, (_, i) => ({ a: i }))
    render(<ContributedViewCard view={view({ value: { rows }, schema: { kind: 'table', path: ['rows', 'a'] } })} />)
    expect(screen.getByTestId('contributed-more').textContent).toBe('+3 more')
  })

  it('labels keyvalue rows by the final path segment, not the raw selector', () => {
    render(
      <ContributedViewCard
        view={view({
          value: { pings: { last: 'iad-01', total: 42 } },
          schema: { kind: 'keyvalue', path: ['pings.last', 'pings.total'] },
        })}
      />,
    )
    const dump = screen.getByTestId('contributed-keyvalue')
    expect(dump.textContent).toContain('last')
    expect(dump.textContent).toContain('total')
    // The dotted selector syntax is not exposed as a label.
    expect(dump.textContent).not.toContain('pings.last')
  })

  it('does not execute a value that looks like markup', () => {
    render(<ContributedViewCard view={view({ value: { x: '<img onerror=alert(1)>' } })} />)
    // React escapes it; the text is present and no element was created from it.
    expect(screen.getByTestId('contributed-keyvalue').querySelector('img')).toBeNull()
  })
})

describe('ContributedViews', () => {
  it('renders nothing when the member has no contributed views', () => {
    const { container } = render(<ContributedViews views={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one card per view', () => {
    render(
      <ContributedViews
        views={[view(), view({ key: 'other/thing', value: 1 })]}
      />,
    )
    expect(screen.getByTestId('contributed-card-demoapp/count')).toBeTruthy()
    expect(screen.getByTestId('contributed-card-other/thing')).toBeTruthy()
  })
})

describe('the store rules a contributed row depends on', () => {
  beforeEach(() => memberProjectionStore.clear())

  it('seeds a contributed row at its OWN seq, not at asOfSeq', () => {
    memberProjectionStore.seed(
      SLUG,
      { roster: { name: 'x' }, 'demoapp/count': { n: 1 } },
      12,
      { 'demoapp/count': 3 },
    )
    // The contributor's next push is seq 4 -- higher than its row's 3, so it
    // wins. Seeding at asOfSeq 12 would have dropped it.
    memberProjectionStore.apply(SLUG, 'demoapp/count', { n: 2 }, 4)
    expect(memberProjectionStore.get(SLUG, 'demoapp/count')).toEqual({ n: 2 })
  })

  it('keeps a schema across a later value push', () => {
    memberProjectionStore.apply(SLUG, 'demoapp/count', 1, 1, { kind: 'badge' })
    memberProjectionStore.apply(SLUG, 'demoapp/count', 2, 2)
    expect(memberProjectionStore.schemaOf(SLUG, 'demoapp/count')).toEqual({ kind: 'badge' })
  })

  it('lists only namespaced keys as contributed, sorted', () => {
    memberProjectionStore.apply(SLUG, 'roster', { name: 'x' }, 1)
    memberProjectionStore.apply(SLUG, 'zapp/b', 1, 1)
    memberProjectionStore.apply(SLUG, 'aapp/a', 1, 1)
    expect(memberProjectionStore.contributedViews(SLUG).map((v) => v.key)).toEqual([
      'aapp/a',
      'zapp/b',
    ])
  })

  it('omits a row whose value is null so a teardown frame removes the card', () => {
    memberProjectionStore.apply(SLUG, 'demoapp/count', 1, 1)
    expect(memberProjectionStore.contributedViews(SLUG)).toHaveLength(1)
    // The §6 deletion frame: value null at a seq past any real fold.
    memberProjectionStore.apply(SLUG, 'demoapp/count', null, 2 ** 53 - 1)
    expect(memberProjectionStore.contributedViews(SLUG)).toHaveLength(0)
  })

  it('re-enables a torn-down card even at a lower seq than the teardown', () => {
    // A teardown removes the row rather than parking a null at a max seq, so a
    // contributor that re-enables at its own (lower) fold seq is not dropped by
    // higher-seq-wins against a lingering teardown seq.
    memberProjectionStore.apply(SLUG, 'demoapp/count', 1, 1)
    memberProjectionStore.apply(SLUG, 'demoapp/count', null, 2 ** 53 - 1)
    expect(memberProjectionStore.contributedViews(SLUG)).toHaveLength(0)
    memberProjectionStore.apply(SLUG, 'demoapp/count', 7, 2)
    const views = memberProjectionStore.contributedViews(SLUG)
    expect(views).toHaveLength(1)
    expect(views[0].value).toBe(7)
  })

  it('notifies the contributed face on appear, value update, and teardown', () => {
    let fired = 0
    const face = memberProjectionStore.contributedFace(SLUG)
    const stop = face.subscribe(() => {
      fired += 1
    })
    // Appears.
    memberProjectionStore.apply(SLUG, 'demoapp/count', 1, 1)
    expect(fired).toBe(1)
    // A value update to an EXISTING contributed card must re-render the list:
    // useMemberContributedViews recomputes only on the keyset version.
    memberProjectionStore.apply(SLUG, 'demoapp/count', 2, 2)
    expect(fired).toBe(2)
    // Teardown re-renders the list too (the card disappears).
    memberProjectionStore.apply(SLUG, 'demoapp/count', null, 2 ** 53 - 1)
    expect(fired).toBe(3)
    stop()
  })

  it('does not notify the contributed face for a bare (built-in) key update', () => {
    let fired = 0
    const face = memberProjectionStore.contributedFace(SLUG)
    const stop = face.subscribe(() => {
      fired += 1
    })
    // A bare key is a built-in projection, not a contributed card; a value
    // update to it is not a change to the contributed key set.
    memberProjectionStore.apply(SLUG, 'roster', { name: 'a' }, 1)
    expect(fired).toBe(1) // first appearance still bumps the version
    memberProjectionStore.apply(SLUG, 'roster', { name: 'b' }, 2)
    expect(fired).toBe(1)
    stop()
  })
})
