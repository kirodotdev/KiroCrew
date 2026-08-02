import { createElement } from 'react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Command, MessageSquarePlus, SunMoon, Keyboard, Pin } from 'lucide-react'

import { useAppDispatch, useAppSelector } from '../../../store'
import { createSlot } from '../../../store/chatSlice'
import { useSessionActions } from '../../../hooks/useSessionActions'
import { useTheme } from '../../../hooks/useTheme'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import type { ResourceProvider, Result } from '../types'

import { i18nT } from '../../../i18n/t'

/**
 * Actions provider (Search Everywhere).
 *
 * Backs the **Actions** tab with a small, static list of global commands
 * (New session, Toggle theme, Open Shortcuts) rather than a remote data
 * source. Each action maps to a {@link Result} whose `onActivate` simply runs
 * the action.
 *
 * Per the §2 Enter matrix, Actions are pure command-invocations: Enter runs
 * the action and there is no ⌘Enter (new session) or ⌥Enter (preview) variant,
 * so `onCmdActivate` / `onAltActivate` are intentionally left unset.
 *
 * The concrete provider ({@link createActionsProvider}) takes its side effects
 * as injected callbacks so it stays free of React hooks and is unit-testable
 * with plain spies. {@link useActionsProvider} wires the real implementations:
 * `New session` dispatches the `createSlot` chat thunk, `Toggle theme` calls
 * the theme context's `cycle()` (light → dark → system), and `Open Shortcuts`
 * is supplied by the palette host (the `ShortcutsModal` open state lives in the
 * app shell, mirroring `useKeyboardShortcuts`'s `onToggleShortcutsModal`).
 */

const PROVIDER_ID = 'actions'
/** Catalog KEY for the tab label — not the label itself. This is module scope,
 *  evaluated once at import, so an `i18nT()` call here would freeze the boot
 *  language; the call sits in `createActionsProvider()` below. The label is also
 *  what a typed scope prefix matches against in CommandPalette, so localising it
 *  localises the scope shortcut too (type the translated word, then Tab). */
const PROVIDER_LABEL_KEY = 'components.commandPalette.providers.actionsProvider.actions'

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function inlineIcon(Icon: typeof Command): ReactNode {
  return createElement(Icon, { className: 'lucide-inline' })
}

/** A single global action before scoring. */
interface ActionDef {
  /** Stable key, used to build the result id. */
  key: string
  title: string
  /** Optional secondary line describing what the action does. */
  subtitle?: string
  icon: ReactNode
  /** The side effect to run on activation (Enter). */
  run: () => void
}

/**
 * Side effects the Actions provider invokes. Injected so the provider has no
 * hook dependencies and can be tested with plain mock functions.
 */
export interface ActionsProviderDeps {
  /** Start a fresh chat session (Enter on "New session"). */
  newSession: () => void
  /** Cycle the color mode (light → dark → system). */
  toggleTheme: () => void
  /** Open the keyboard-shortcuts help modal. */
  openShortcuts: () => void
  /** Pin state + toggle for the active session; null when no session is active (action omitted). */
  pinCurrentSession?: { pinned: boolean; toggle: () => void } | null
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Build the Actions {@link ResourceProvider} from injected side effects. Pure
 * (no React hooks) so it can be exercised directly in tests.
 *
 * Matching is done with {@link fuzzyMatch} over the action titles. An empty
 * query yields a neutral score for every action, so the palette shows the full
 * action list before the user types anything.
 */
export function createActionsProvider(deps: ActionsProviderDeps): ResourceProvider {
  const actions: ActionDef[] = [
    {
      key: 'new-session',
      title: 'New session',
      subtitle: i18nT('components.commandPalette.providers.actionsProvider.start_a_fresh_chat'),
      icon: inlineIcon(MessageSquarePlus),
      run: deps.newSession,
    },
    {
      key: 'toggle-theme',
      title: 'Toggle theme',
      subtitle: i18nT('components.commandPalette.providers.actionsProvider.cycle_light_dark_system'),
      icon: inlineIcon(SunMoon),
      run: deps.toggleTheme,
    },
    {
      key: 'open-shortcuts',
      title: 'Open Shortcuts',
      subtitle: i18nT('components.commandPalette.providers.actionsProvider.keyboard_shortcuts_help'),
      icon: inlineIcon(Keyboard),
      run: deps.openShortcuts,
    },
  ]

  if (deps.pinCurrentSession) {
    const { pinned, toggle } = deps.pinCurrentSession
    actions.push({
      key: 'pin-session',
      title: pinned
        ? i18nT('components.commandPalette.providers.actionsProvider.unpin_current_session')
        : i18nT('components.commandPalette.providers.actionsProvider.pin_current_session'),
      subtitle: pinned
        ? i18nT('components.commandPalette.providers.actionsProvider.remove_pin_from_this_chat')
        : i18nT('components.commandPalette.providers.actionsProvider.pin_this_chat_to_the_top'),
      icon: inlineIcon(Pin),
      run: toggle,
    })
  }

  return {
    id: PROVIDER_ID,
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, so `label: i18nT(...)` would resolve
    // once and keep the pre-switch wording forever. `LanguageProvider` forces a
    // re-RENDER via `cloneElement` (it deliberately does NOT remount — see its own
    // comment rejecting `key={active}`), and a re-render does not recompute a memo.
    // An accessor moves the lookup to the consumer's render, where the tab strip
    // reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: inlineIcon(Command),
    search(query: string): Result[] {
      const results: Result[] = []
      for (const action of actions) {
        const match = fuzzyMatch(query, action.title)
        if (!match) continue
        results.push({
          id: `${PROVIDER_ID}:${action.key}`,
          providerId: PROVIDER_ID,
          title: action.title,
          subtitle: action.subtitle,
          icon: action.icon,
          score: match.score,
          indices: match.indices,
          // Declarative §2 Enter action (/ task 27): Actions are pure
          // command-invocations — Enter runs `action.run`, and ⌘Enter has no
          // distinct behavior (the dispatcher ignores the modifier for this
          // kind). `run` is carried on the action payload so the dispatcher can
          // invoke it directly; `onActivate` mirrors it for the legacy path.
          enter: { kind: 'invoke', run: action.run },
          onActivate: action.run,
        })
      }
      results.sort(compareResults)
      return results
    },
  }
}

/**
 * React hook: an Actions provider wired to the chat store and theme context.
 *
 * @param opts.openShortcuts - Opens the shortcuts help modal. Supplied by the
 *   palette host because the `ShortcutsModal` open state is owned by the app
 *   shell, not a global store slice (mirrors `useKeyboardShortcuts`).
 */
export function useActionsProvider(opts: { openShortcuts: () => void }): ResourceProvider {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const { cycle } = useTheme()
  const { openShortcuts } = opts

  const { togglePin } = useSessionActions()
  const activeSlot = useAppSelector((s) => s.chat.activeSlot)
  const activePinned = useAppSelector(
    (s) => s.dashboard.slots.find((slot) => slot.key === activeSlot)?.pinned ?? false,
  )

  // "New session" is a write (createSlot); route it through useMutation for
  // error/loading state and consistency with paletteActions.ts's createSlot
  // mutation (`use-react-query` lint rule). onSuccess navigates to /chat so the
  // user lands in the new session rather than staying on the current page.
  const { mutate: doNewSession } = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onSuccess: () => navigate('/chat'),
  })

  return useMemo(
    () =>
      createActionsProvider({
        newSession: () => doNewSession(),
        toggleTheme: () => {
          cycle()
        },
        openShortcuts,
        pinCurrentSession: activeSlot
          ? { pinned: activePinned, toggle: () => togglePin(activeSlot) }
          : null,
      }),
    [doNewSession, cycle, openShortcuts, activeSlot, activePinned, togglePin],
  )
}
