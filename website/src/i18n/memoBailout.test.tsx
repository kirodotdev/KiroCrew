/**
 * D11 probe: does a `memo()`-wrapped child survive a language change?
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
 * standalone `i18nT()` subscribes to nothing, a bailed-out subtree keeps rendering
 * the previous catalog.
 *
 * 40 files in `website/src` combine `memo()` with `i18nT()`. `components/PastedChip.tsx`
 * is the clean case: `export default memo(PastedChip)`, it calls
 * `i18nT('components.pastedChip.paste')`, and `pages/ChatPage.tsx` renders it as
 * `<PastedChip block={r.block} />` — a prop that does not change when the language does.
 *
 * This file reproduces that shape in isolation. It is a diagnostic, not a guard:
 * if the memoized assertion fails, D11 is a live defect and the fix belongs in
 * `t.ts` / `LanguageProvider.tsx`, not here.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { memo } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { LanguageProvider, useLanguage } from './LanguageProvider'
import { i18nT } from './t'
import { api } from '../api/client'

const KEY = 'pages.settings.displayPanel.view'

/** Leaf that reads the catalog through the standalone `i18nT` path. */
function Leaf({ testid }: { testid: string }) {
  return <span data-testid={testid}>{i18nT(KEY)}</span>
}

/**
 * The memo boundary. `block` is a stable object created once at module scope, so
 * it is referentially identical across a language change — mirroring
 * `<PastedChip block={r.block} />`.
 */
const MemoLeaf = memo(function MemoLeaf({ block }: { block: { id: number } }) {
  return <span data-testid="memoized">{i18nT(KEY)}{block.id}</span>
})

const STABLE_BLOCK = { id: 1 }

function Tree() {
  const { setLanguage } = useLanguage()
  return (
    <div>
      {/* control: not memoized, known to work */}
      <Leaf testid="plain" />
      {/* subject: memoized with a stable prop */}
      <MemoLeaf block={STABLE_BLOCK} />
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

describe('D11 — memo() boundary under a language change', () => {
  it('both leaves start in English', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('plain')).toHaveTextContent('View'))
    expect(screen.getByTestId('memoized')).toHaveTextContent('View')
  })

  it('the non-memoized leaf switches to Chinese (control)', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('plain')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    await waitFor(() => {
      expect(screen.getByTestId('plain')).not.toHaveTextContent('View')
    })
  })

  /**
   * `it.fails` because this is a KNOWN DEFECT, not an aspiration.
   *
   * The suite stays green while the bug exists, and turns red the moment someone
   * fixes it — at which point flip this to `it()` and delete this comment. That is
   * the opposite of `skip`, which would let the fix land unnoticed and let the
   * defect silently return later.
   *
   * The fix is not in this file: make the language a tracked input, either by
   * having `i18nT` subscribe (`useSyncExternalStore` over i18next's
   * `languageChanged` event) or by routing call sites through `useTranslation`.
   */
  it.fails('the memoized leaf does NOT switch — D11, known defect', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('memoized')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    // Wait for the control to prove the switch landed, so a failure below is
    // attributable to the memo boundary and not to a slow catalog load.
    await waitFor(() => expect(screen.getByTestId('plain')).not.toHaveTextContent('View'))
    expect(screen.getByTestId('memoized')).not.toHaveTextContent('View')
  })
})
