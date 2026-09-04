import { createElement } from 'react'
import { ScrollText } from 'lucide-react'
import { useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { usePaletteActions } from '../paletteActions'
import { i18nT } from '../../../i18n/t'
import type { Result, ResourceProvider } from '../types'

/**
 * Prompts provider for the Search Everywhere command palette.
 *
 * Backs the **Prompts** tab with the saved prompts & Agent SOPs returned by
 * `api.prompts()` (the `/api/prompts` endpoint that {@link PromptsTab} renders
 * in the overview). Each prompt maps to a {@link Result}:
 *  - `onActivate` (Enter) — **context-aware** (§2 matrix): insert `@<fullName>`
 *    into the active chat composer (mirrors `PromptsTab`'s
 *    `setPendingInput('@'+fullName)`), or — when no chat is active — open a new
 *    session seeded with the token.
 *  - `onCmdActivate` (⌘Enter) — always open a new session seeded with the prompt.
 *  - `onAltActivate` (⌥Enter) — **preview** the prompt (defaults to opening the
 *    skills/prompts catalog; host-overridable).
 *
 * Matching is a client-side {@link fuzzyMatch} over the prompt **name**. The
 * server returns the full prompt list, cached under the
 * React-Query key `['prompts']` (shared with `PromptsTab` and the inline
 * `@`-picker) so reopening the palette is free. Highlight `indices` index into
 * the rendered `title` and are emitted as React `<mark>` nodes by the palette,
 * never as HTML strings (`frontend-security` lint rule).
 */

const PROVIDER_ID = 'prompts'

/**
 * Catalog KEY for the palette tab label, not the label itself.
 *
 * The provider object is built inside a `useMemo` whose deps do not include the
 * language, so `label: i18nT(…)` would resolve once and keep the pre-switch
 * wording. It is exposed as a GETTER below instead, which re-resolves on every
 * read — `CommandPalette` reads `provider.label` during render (scope hint, Tab
 * completion), so the getter runs per render without the memo having to know
 * about i18n. `PROVIDER_ID` stays a code constant: it is the provider's
 * identity, matched against persisted palette scope.
 */
const PROVIDER_LABEL_KEY = 'components.commandPalette.providers.promptsProvider.prompts'

/** Cache the prompt list briefly; reuses the `['prompts']` React-Query entry. */
const PROMPTS_STALE_MS = 30_000

/**
 * One prompt as returned by `/api/prompts`. `api.prompts` is loosely typed at
 * the client layer, so we pin the fields the provider reads. Matches the
 * `Prompt` shape used by `PromptsTab`.
 */
export interface PromptItem {
  name: string
  fullName: string
  description?: string
  path?: string
  package?: string
  source?: string
}

/** A prompt reference passed to the activation callbacks. */
export interface PromptRef {
  name: string
  fullName: string
  path?: string
}

/**
 * Injectable dependencies for {@link createPromptsProvider}. Keeping the
 * concrete provider free of React hooks makes it unit-testable with a plain
 * mock fetch + callbacks; {@link usePromptsProvider} wires the real
 * React-Query + Redux implementations.
 */
export interface PromptsProviderDeps {
  /** Fetch the full prompt list (React-Query-cached in the hook). */
  fetchPrompts: () => Promise<PromptItem[]>
  /** Insert the prompt into the active composer (Enter). */
  insertPrompt: (ref: PromptRef) => void
  /** Start a new session seeded with the prompt (⌘Enter). Optional. */
  newSessionWithPrompt?: (ref: PromptRef) => void
  /** Preview the prompt (⌥Enter). Optional. */
  previewPrompt?: (ref: PromptRef) => void
}

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function promptIcon() {
  return createElement(ScrollText, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Build the Prompts {@link ResourceProvider} from injected dependencies. Pure
 * (no React hooks) so it can be exercised directly in tests.
 *
 * An empty query yields a neutral {@link fuzzyMatch} score for every prompt, so
 * the palette shows the full prompt list before the user types anything.
 */
export function createPromptsProvider(deps: PromptsProviderDeps): ResourceProvider {
  const { fetchPrompts, insertPrompt, newSessionWithPrompt, previewPrompt } = deps

  return {
    id: PROVIDER_ID,
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: promptIcon(),
    async search(query: string): Promise<Result[]> {
      const prompts = (await fetchPrompts()) ?? []

      const results: Result[] = []
      for (const p of prompts) {
        const title = p.name
        // Fuzzy-match over the prompt name; indices align with title.
        const match = fuzzyMatch(query, title)
        if (!match) continue
        const ref: PromptRef = { name: p.name, fullName: p.fullName, path: p.path }
        results.push({
          id: `${PROVIDER_ID}:${p.fullName || p.name}`,
          providerId: PROVIDER_ID,
          title,
          subtitle: p.description || p.package || undefined,
          icon: promptIcon(),
          score: match.score,
          indices: match.indices,
          onActivate: () => insertPrompt(ref),
          onCmdActivate: newSessionWithPrompt ? () => newSessionWithPrompt(ref) : undefined,
          onAltActivate: previewPrompt ? () => previewPrompt(ref) : undefined,
        })
      }

      results.sort(compareResults)
      return results
    },
  }
}

/**
 * React hook that returns a live Prompts provider wired to React-Query and the
 * chat store.
 *
 * Per the `use-react-query` lint rule the prompt list is fetched through
 * React-Query under the key `['prompts']` (identical to `PromptsTab` and the
 * inline `@`-picker, so the cache is shared). `queryClient.fetchQuery` is used
 * rather than `useQuery` because a {@link ResourceProvider}'s `search` is an
 * imperative call from the palette, not a render-time subscription.
 *
 * The default `insertPrompt` is context-aware (insert into the active chat, or
 * open a new session when there is none) via {@link usePaletteActions}; ⌘Enter
 * seeds a new session and ⌥Enter previews. All are host-overridable via `opts`.
 *
 * @param opts.insertPrompt - Overrides the context-aware primary-Enter handler.
 * @param opts.newSessionWithPrompt - ⌘Enter: new session seeded with the prompt.
 * @param opts.previewPrompt - ⌥Enter: preview the prompt.
 */
export function usePromptsProvider(opts?: {
  insertPrompt?: (ref: PromptRef) => void
  newSessionWithPrompt?: (ref: PromptRef) => void
  previewPrompt?: (ref: PromptRef) => void
}): ResourceProvider {
  const queryClient = useQueryClient()
  const actions = usePaletteActions()
  const insertPromptOverride = opts?.insertPrompt
  const newSessionOverride = opts?.newSessionWithPrompt
  const previewOverride = opts?.previewPrompt
  const { enterInsertOrNewSession, newSessionWithToken, navigate } = actions

  return useMemo(
    () =>
      createPromptsProvider({
        fetchPrompts: () =>
          queryClient.fetchQuery<PromptItem[]>({
            queryKey: ['prompts'],
            queryFn: () => api.prompts() as Promise<PromptItem[]>,
            staleTime: PROMPTS_STALE_MS,
          }),
        // §2 matrix: Enter is context-aware (insert into the active chat, else
        // open a new session); ⌘Enter is always new-session; ⌥Enter previews.
        insertPrompt:
          insertPromptOverride ?? ((ref) => enterInsertOrNewSession(`@${ref.fullName}`)),
        newSessionWithPrompt:
          newSessionOverride ?? ((ref) => newSessionWithToken(`@${ref.fullName}`)),
        previewPrompt: previewOverride ?? (() => navigate('/capabilities')),
      }),
    [
      queryClient,
      enterInsertOrNewSession,
      newSessionWithToken,
      navigate,
      insertPromptOverride,
      newSessionOverride,
      previewOverride,
    ],
  )
}
