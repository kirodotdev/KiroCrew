import { createElement } from 'react'
import { Sparkles } from 'lucide-react'
import { useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { resolveInvokableEnter, usePaletteActions } from '../paletteActions'
import type { Result, ResourceProvider } from '../types'

/**
 * Skills provider for the Search Everywhere command palette.
 *
 * Backs the **Skills** tab with the skills returned by `api.skills()` (the
 * `/api/skills` endpoint, the same source `SkillsTab` and the inline
 * `$`-picker use). Each skill maps to a {@link Result} per the §2 Enter matrix:
 *  - `onActivate` (Enter) — **context-aware**: insert `$<name>` into the active
 *    chat composer, or — when no chat is active — open a new session seeded
 *    with the token.
 *  - `onCmdActivate` (⌘Enter) — always open a new session seeded with `$<name>`.
 *  - `onAltActivate` (⌥Enter) — open the skill's SKILL.md (read / preview).
 *
 * ## Security (Input Validation)
 *
 * The provider only ever emits the plain token `$<name>` and routes it through
 * the existing chat-submit path (`setPendingInput` / `createSlot`). Server-side
 * `SkillsLoader.get_triggered_skills()` / `load_skill()` — guarded by
 * `_safe_name()` (rejects `..` / `\\`) and `validate_file_path()` — does the
 * allowlisted resolution. No filesystem path is built from user input here and
 * no second resolution path is introduced (see `paletteActions.ts`).
 *
 * Matching is a client-side {@link fuzzyMatch} over the skill **name**. The
 * list is cached under the React-Query key `['skills']` (shared
 * with `SkillsTab` / the inline `$`-picker) so reopening the palette is free.
 * Highlight `indices` index into the rendered `title` and are emitted as React
 * nodes by the palette, never as HTML strings (`frontend-security` lint rule).
 */

const PROVIDER_ID = 'skills'

/**
 * Catalog KEY for the palette tab label — resolved by the `label` getter below,
 * not here. This constant is initialised at module load, so an `i18nT()` call at
 * this position would freeze whatever language was active at boot.
 */
const PROVIDER_LABEL_KEY = 'components.commandPalette.providers.skillsProvider.skills'

/** Cache the skill list briefly; reuses the `['skills']` React-Query entry. */
const SKILLS_STALE_MS = 30_000

/** Where ⌥Enter ("open SKILL.md") navigates — the skills/prompts catalog. */
const SKILLS_ROUTE = '/capabilities'

/**
 * One skill as returned by `/api/skills`. `api.skills` is loosely typed at the
 * client layer, so we pin the fields the provider reads. Matches the `Skill`
 * shape used by `SkillsTab`.
 */
export interface SkillItem {
  name: string
  description?: string
  triggers?: string
  path?: string
  category?: string
  always?: boolean
}

/** A skill reference passed to the preview callback. */
export interface SkillRef {
  name: string
}

/**
 * Injectable dependencies for {@link createSkillsProvider}. Keeping the
 * concrete provider free of React hooks makes it unit-testable with a plain
 * mock fetch + callbacks; {@link useSkillsProvider} wires the real React-Query +
 * store implementations.
 */
export interface SkillsProviderDeps {
  /** Fetch the full skill list (React-Query-cached in the hook). */
  fetchSkills: () => Promise<SkillItem[]>
  /** True when a chat session is active (selects insert vs new-session Enter). */
  hasActiveChat: boolean
  /** Insert a token into the active composer. */
  insertToken: (token: string) => void
  /** Open a new session seeded with the token. */
  newSessionWithToken: (token: string) => void
  /** Open the skill's SKILL.md / preview (⌥Enter). */
  openSkillDoc: (ref: SkillRef) => void
}

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function skillIcon() {
  return createElement(Sparkles, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Build the Skills {@link ResourceProvider} from injected dependencies. Pure
 * (no React hooks) so it can be exercised directly in tests.
 *
 * An empty query yields a neutral {@link fuzzyMatch} score for every skill, so
 * the palette shows the full skill list before the user types anything.
 */
export function createSkillsProvider(deps: SkillsProviderDeps): ResourceProvider {
  const { fetchSkills, hasActiveChat, insertToken, newSessionWithToken, openSkillDoc } = deps

  return {
    id: PROVIDER_ID,
    // A getter, not a value: the provider object is built once inside
    // `useSkillsProvider`'s `useMemo`, whose deps do not include the language, so
    // a plain `label: i18nT(…)` would resolve once per mount. Reading it through
    // an accessor moves the lookup to the consumer's render, which is where the
    // tab strip reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: skillIcon(),
    async search(query: string): Promise<Result[]> {
      const skills = (await fetchSkills()) ?? []

      const results: Result[] = []
      // Dedup by name: /api/skills can return the same skill more than once
      // when a shared package skills dir is enumerated by multiple source loaders
      // (e.g. source "kirocrew" + "aim"). Keep the first occurrence.
      const seen = new Set<string>()
      for (const s of skills) {
        if (seen.has(s.name)) continue
        seen.add(s.name)
        const title = s.name
        // Fuzzy-match over the skill name; indices align with title.
        const match = fuzzyMatch(query, title)
        if (!match) continue
        const token = `$${s.name}`
        const ref: SkillRef = { name: s.name }
        results.push({
          id: `${PROVIDER_ID}:${s.name}`,
          providerId: PROVIDER_ID,
          title,
          subtitle: s.description || s.category || undefined,
          icon: skillIcon(),
          score: match.score,
          indices: match.indices,
          // Declarative Enter contract (§2). The central
          // `dispatchEnter` in CommandPalette routes on this: primary Enter is
          // context-aware (insert `$<skill>` into the active composer, else new
          // seeded session) and ⌘Enter is always a new seeded session, both
          // keyed off `token`. `source` (the SKILL.md path) backs ⌥Enter
          // preview. The `on*Activate` closures below back the mouse-execution
          // + fallback path.
          enter: { kind: 'insert-token', token, tokenKind: 'skill', source: s.path },
          // §2 matrix: Enter is context-aware; ⌘Enter is always new-session;
          // ⌥Enter opens the doc. The resolver is invoked at *activation* time
          // (deferred thunk) — not eagerly while building the list — so merely
          // listing or previewing a skill never routes through the allowlist
          // resolver (input validation: resolution is an activation, not a side effect of
          // rendering a row).
          onActivate: () =>
            resolveInvokableEnter(hasActiveChat, token, {
              insertToken,
              newSessionWithToken,
            })(),
          onCmdActivate: () => newSessionWithToken(token),
          onAltActivate: () => openSkillDoc(ref),
        })
      }

      results.sort(compareResults)
      return results
    },
  }
}

/**
 * React hook that returns a live Skills provider wired to React-Query and the
 * chat store via {@link usePaletteActions}.
 *
 * Per the `use-react-query` lint rule the skill list is fetched through
 * React-Query under the key `['skills']` (identical to `SkillsTab` and the
 * inline `$`-picker, so the cache is shared). `queryClient.fetchQuery` is used
 * rather than `useQuery` because a {@link ResourceProvider}'s `search` is an
 * imperative call from the palette, not a render-time subscription.
 *
 * @param opts.openSkillDoc - Overrides ⌥Enter (open SKILL.md). Defaults to
 *   navigating to the skills/prompts catalog (no per-skill deep link exists).
 */
export function useSkillsProvider(opts?: {
  openSkillDoc?: (ref: SkillRef) => void
}): ResourceProvider {
  const queryClient = useQueryClient()
  const actions = usePaletteActions()
  const openSkillDocOverride = opts?.openSkillDoc

  const { hasActiveChat, insertToken, newSessionWithToken, navigate } = actions

  return useMemo(
    () =>
      createSkillsProvider({
        fetchSkills: () =>
          queryClient.fetchQuery<SkillItem[]>({
            queryKey: ['skills'],
            queryFn: ({ signal }) => api.skills(undefined, undefined, signal) as Promise<SkillItem[]>,
            staleTime: SKILLS_STALE_MS,
          }),
        hasActiveChat,
        insertToken,
        newSessionWithToken,
        openSkillDoc:
          openSkillDocOverride ??
          (() => {
            // No per-skill deep link exists yet; open the catalog page.
            navigate(SKILLS_ROUTE)
          }),
      }),
    [
      queryClient,
      hasActiveChat,
      insertToken,
      newSessionWithToken,
      navigate,
      openSkillDocOverride,
    ],
  )
}
