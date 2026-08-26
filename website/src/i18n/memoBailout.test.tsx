/**
 * Guard: a `memo()`-wrapped descendant repaints on a language change.
 *
 * `renderSwitch.test.tsx` proves the top of the tree repaints. It cannot prove
 * anything about a memoized DESCENDANT, because its `Sample` component is the
 * direct child of `LanguageProvider` — exactly the one node the provider hands a
 * fresh element to.
 *
 * The mechanism under test: `LanguageProvider` forces a repaint with
 * `useMemo(() => cloneElement(children), [children, active])`. That produces a new
 * element for the immediate child only. React then reconciles downward, and any
 * `memo()` boundary whose props are shallow-equal short-circuits the subtree. Since
 * standalone `i18nT()` subscribes to nothing, a bailed-out subtree would keep
 * rendering the previous catalog.
 *
 * What closes the gap: every production component that combines `memo()` with
 * `i18nT()` calls `useLanguageGeneration()` once at the top of its body. The
 * `useSyncExternalStore` subscription schedules that component's own re-render on
 * `languageChanged`, which the memo props comparison cannot suppress. `MemoLeaf`
 * below mirrors that fixed shape (`components/PastedChip.tsx` is the archetype:
 * `export default memo(PastedChip)` with a stable `block` prop), and
 * `memoSubscription.ratchet.test.ts` pins that every such production file
 * actually carries the hook.
 *
 * `BareMemoLeaf` keeps the ORIGINAL defect shape (no hook) and asserts it stays
 * stale. That is not an endorsement — it proves the hook is load-bearing: if a
 * refactor ever makes the bare shape pass (e.g. the provider starts remounting,
 * which would destroy state), or someone deletes the hook believing it inert,
 * one of these two tests turns red.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { memo, useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Registers the zh-CN catalog. `./index` — what the vitest setup file inits — is
// English only, and a switch that silently falls back to English would leave the
// control leaf reading 'View' too, so the assertions below would pass for the
// wrong reason and hide the defect they guard against.
import './all'
import { LanguageProvider, useLanguage } from './LanguageProvider'
import { i18nT } from './t'
import { useLanguageGeneration } from './useLanguageGeneration'
import { api } from '../api/client'

const KEY = 'pages.settings.displayPanel.view'

/** Leaf that reads the catalog through the standalone `i18nT` path. */
function Leaf({ testid }: { testid: string }) {
  return <span data-testid={testid}>{i18nT(KEY)}</span>
}

/**
 * The memo boundary in its FIXED production shape. `block` is a stable object
 * created once at module scope, so it is referentially identical across a
 * language change — mirroring `<PastedChip block={r.block} />`. Only the
 * `useLanguageGeneration()` subscription makes this repaint.
 */
const MemoLeaf = memo(function MemoLeaf({ block }: { block: { id: number } }) {
  useLanguageGeneration()
  return <span data-testid="memoized">{i18nT(KEY)}{block.id}</span>
})

/** The ORIGINAL defect shape: memo + i18nT with no subscription. */
const BareMemoLeaf = memo(function BareMemoLeaf({ block }: { block: { id: number } }) {
  return <span data-testid="bare-memoized">{i18nT(KEY)}{block.id}</span>
})

/**
 * Stateful memoized leaf: proves the repaint is a RE-RENDER, not a remount.
 * `cloneElement` (not a `key` change) is what the provider uses precisely so
 * component state survives a switch; if anything in the chain regressed to
 * remounting, the count would reset to 0 here.
 */
const StatefulMemoLeaf = memo(function StatefulMemoLeaf({ block }: { block: { id: number } }) {
  useLanguageGeneration()
  const [count, setCount] = useState(0)
  return (
    <span data-testid="stateful">
      <button onClick={() => setCount(c => c + 1)}>inc</button>
      {i18nT(KEY)}:{count}:{block.id}
    </span>
  )
})

const STABLE_BLOCK = { id: 1 }

function Tree() {
  const { setLanguage } = useLanguage()
  return (
    <div>
      {/* control: not memoized, known to work */}
      <Leaf testid="plain" />
      {/* subject: memoized with a stable prop, carrying the production fix */}
      <MemoLeaf block={STABLE_BLOCK} />
      {/* counter-subject: the unfixed shape, must stay stale */}
      <BareMemoLeaf block={STABLE_BLOCK} />
      <StatefulMemoLeaf block={STABLE_BLOCK} />
      <button onClick={() => setLanguage('zh-CN')}>zh</button>
    </div>
  )
}

function mount() {
  vi.spyOn(api, 'themeBoot').mockResolvedValue({} as never)
  vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LanguageProvider><Tree /></LanguageProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['en-US'])
})

describe('memo() boundary under a language change', () => {
  it('all leaves start in English', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('plain')).toHaveTextContent('View'))
    expect(screen.getByTestId('memoized')).toHaveTextContent('View')
    expect(screen.getByTestId('bare-memoized')).toHaveTextContent('View')
  })

  it('the non-memoized leaf switches to Chinese (control)', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('plain')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    await waitFor(() => {
      expect(screen.getByTestId('plain')).not.toHaveTextContent('View')
    })
  })

  it('the memoized leaf switches — useLanguageGeneration() defeats the bailout', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('memoized')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    // Wait for the control to prove the switch landed, so a failure below is
    // attributable to the memo boundary and not to a slow catalog load.
    await waitFor(() => expect(screen.getByTestId('plain')).not.toHaveTextContent('View'))
    await waitFor(() => expect(screen.getByTestId('memoized')).not.toHaveTextContent('View'))
  })

  it('the bare memoized leaf stays stale — the hook is load-bearing, not decoration', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('bare-memoized')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    await waitFor(() => expect(screen.getByTestId('plain')).not.toHaveTextContent('View'))
    // Give the subscribed leaves' re-render a chance to flush, then assert the
    // unsubscribed boundary did NOT repaint.
    await waitFor(() => expect(screen.getByTestId('memoized')).not.toHaveTextContent('View'))
    expect(screen.getByTestId('bare-memoized')).toHaveTextContent('View')
  })

  it('a stateful memoized leaf keeps its state across a switch — nothing remounts', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('stateful')).toHaveTextContent('View:0:1'))
    await userEvent.click(screen.getByText('inc'))
    await userEvent.click(screen.getByText('inc'))
    expect(screen.getByTestId('stateful')).toHaveTextContent('View:2:1')
    await userEvent.click(screen.getByText('zh'))
    await waitFor(() => expect(screen.getByTestId('plain')).not.toHaveTextContent('View'))
    await waitFor(() => expect(screen.getByTestId('stateful')).not.toHaveTextContent('View'))
    // The count survived: the repaint was a re-render, not a remount.
    expect(screen.getByTestId('stateful')).toHaveTextContent(':2:1')
  })
})
