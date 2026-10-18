import { useMemo } from 'react'
import type { Dispatch, ReactNode, SetStateAction } from 'react'
import {
  Check,
  ChevronsDown,
  Command,
  Component,
  Folder,
  GitMerge,
  MessageSquare,
  MessageSquarePlus,
  Package,
  ScanEye,
  Search,
  Send,
  SunMoon,
  Terminal,
} from 'lucide-react'

import { appNavTargets } from '../../appNav'
import type { AppDispatch, AppStore, RootState } from '../../store'
import { createSlot, switchSlot } from '../../store/chatSlice'
import { focusComposerForOpenedSession } from '../../pages/chat/composerFocus'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel, settingEntryOffered } from '../../components/commandPalette/settingsSearchCore'
import type { SettingsSearchGovernance } from '../../components/commandPalette/settingsSearchCore'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import { settingsSubtitle } from '../../components/commandPalette/settingsTabLabel'
import { appIcon } from '../../components/commandPalette/providers/appsProvider'
import {
  isEmptyNewSlot,
  recencyEpoch,
  sessionStatus,
} from '../../components/commandPalette/providers/recentsProvider'
import { matesIcon } from './matesProvider'
import { i18nT } from '../../i18n/t'

import { SETTING_ROW_PREFIX, type UsageMap } from './frecency'
import { rankRootRows, type RankedRow, type RootRow } from './rootIndex'
import type { ContributedCommand } from './contributedCommands'
import { ATTENTION_MORE_ID, RECENT_SESSION_ROWS, settingRowMatch, type Scope } from './commandBarOverlayModel'
import type { useCommandBarOverlayEngines } from './useCommandBarOverlayEngines'

/**
 * The Command Bar's first page: the launcher rows built from what is already in memory
 * (the sessions that owe the reader something, the ones they were last in, the
 * product's commands, contributed commands, app destinations and settings), ranked
 * against the LIVE query. Issues no request. A new root row or group belongs here.
 */

/**
 * Glyphs a contributed command may name, and the fallback when it names none.
 *
 * An allowlist rather than a URL or inline SVG the app supplies, for two reasons
 * that both matter more than the extra vocabulary: the root promises to issue no
 * request, and a glyph that must be fetched breaks that promise on every open; and
 * an app-supplied SVG is app-authored markup rendered inside the host's own
 * surface. The set is small on purpose and grows by pull request, which is a cheap
 * ask compared to either alternative.
 */
const CONTRIBUTED_ICONS: Record<string, ReactNode> = {
  Check: <Check size={14} className="lucide-inline" />,
  Command: <Command size={14} className="lucide-inline" />,
  GitMerge: <GitMerge size={14} className="lucide-inline" />,
  Package: <Package size={14} className="lucide-inline" />,
  ScanEye: <ScanEye size={14} className="lucide-inline" />,
  Search: <Search size={14} className="lucide-inline" />,
  Send: <Send size={14} className="lucide-inline" />,
  Terminal: <Terminal size={14} className="lucide-inline" />,
}

/** The named glyph, or the generic command one when the name is unknown. */
function contributedIcon(name: string): ReactNode {
  // `Object.hasOwn`, not a plain index with `??`. An INHERITED key is not nullish, so
  // `CONTRIBUTED_ICONS['__proto__']` yields an object and `CONTRIBUTED_ICONS['constructor']`
  // a function -- neither triggers the fallback, and both are then handed to React as a
  // child, which throws. `icon` is deliberately unvalidated beyond being a string ("an
  // unknown name falls back"), so any name reaches this line, and the crash takes the
  // whole overlay down on every open rather than degrading that one row.
  return Object.hasOwn(CONTRIBUTED_ICONS, name) ? (
    CONTRIBUTED_ICONS[name]
  ) : (
    <Terminal size={14} className="lucide-inline" />
  )
}

/** The root's rows, ranked; empty while a scope is entered. */
export function useCommandBarOverlayRootRows({
  liveSlots,
  unreadSlots,
  slotStatusDetail,
  simplifiedToolNames,
  dispatch,
  navigate,
  store,
  cycleTheme,
  crewPreview,
  commandById,
  apps,
  settingsGovernance,
  resolved,
  scope,
  query,
  usage,
  attentionExpanded,
  setAttentionExpanded,
}: {
  liveSlots: RootState['dashboard']['slots']
  unreadSlots: RootState['dashboard']['unreadSlots']
  slotStatusDetail: NonNullable<RootState['chat']['slotStatusDetail']>
  simplifiedToolNames: boolean
  dispatch: AppDispatch
  navigate: (route: string) => void
  store: AppStore
  cycleTheme: () => void
  crewPreview: boolean
  commandById: Map<string, ContributedCommand>
  apps: ReturnType<typeof useCommandBarOverlayEngines>['apps']
  settingsGovernance: SettingsSearchGovernance
  resolved: string
  scope: Scope
  query: string
  usage: UsageMap
  attentionExpanded: boolean
  setAttentionExpanded: Dispatch<SetStateAction<boolean>>
}): RankedRow[] {
  const rootRows: RootRow[] = useMemo(() => {
    const rows: RootRow[] = []
    // The sessions that owe the user something, FIRST.
    //
    // `sessionStatus` marks exactly those with `style: 'pill'` — an approval to
    // grant, a question to answer — and everything else (running, unread, idle) with
    // a dot or nothing. Only the pill cases are lifted here, so this section is
    // usually absent: a launcher that always opens on a "Needs You" header teaches
    // the user to ignore it, and the whole value is that its presence means
    // something. A running session is not waiting on anyone and stays in the
    // sessions view where it belongs.
    const lifted = new Set<string>()
    for (const slot of liveSlots) {
      const st = sessionStatus(slot, unreadSlots, slotStatusDetail[slot.key], simplifiedToolNames)
      if (st.style !== 'pill' || !st.label || !st.colorVar) continue
      lifted.add(slot.key)
      rows.push({
        id: `attention:${slot.key}`,
        title: slot.title || slot.key,
        group: 'attention',
        kind: 'invoke',
        icon: <MessageSquare size={14} className="lucide-inline" />,
        status: { colorVar: st.colorVar, label: st.label, detail: st.detail, pill: true },
        // Same activation the sidebar and the recents listing use, so a session
        // opened from here lands exactly where it lands from anywhere else.
        run: async () => {
          // Not awaited: the bar closes once this promise settles, and a session
          // open closes it at once -- a failed switch is explained by the pane's
          // own notice, not by keeping the bar up. The switch is handed on so the
          // composer is focused only once it has landed (#15732): `pending` enters
          // the slot synchronously, and a caret placed before the gateway answers
          // would file the keystrokes typed meanwhile under a slot a 404 then evicts.
          const switched = dispatch(switchSlot({ key: slot.key, announceOnMissing: true })).unwrap()
          navigate('/chat')
          focusComposerForOpenedSession(switched, slot.key, store)
        },
      })
    }
    // Then the sessions the reader was last in.
    //
    // This is the one thing the surface is opened for most and the one thing it used
    // to answer worst: switching back to yesterday's conversation meant entering the
    // sessions view and typing a name the reader had to remember. The facts are in
    // the same live store the block above reads, so the root pays no request for
    // them — which is the property that decides WHERE this can live. The full corpus
    // stays behind Search Sessions; this is the shortcut, not the index.
    //
    // Empty untitled slots are excluded: switching into a blank chat is what the New
    // Session command is for, and one of those rows is indistinguishable from
    // another. A slot already lifted into `attention` is excluded too — it is on
    // screen, above this, carrying more information than a second copy would.
    const recentSlots = liveSlots
      .filter(slot => !lifted.has(slot.key) && !isEmptyNewSlot(slot))
      .sort((a, b) => recencyEpoch(b) - recencyEpoch(a))
      .slice(0, RECENT_SESSION_ROWS)
    recentSlots.forEach(slot => {
      const st = sessionStatus(slot, unreadSlots, slotStatusDetail[slot.key], simplifiedToolNames)
      rows.push({
        id: `recent:${slot.key}`,
        title: slot.title || slot.key,
        group: 'recent',
        kind: 'invoke',
        icon: <MessageSquare size={14} className="lucide-inline" />,
        // Running state only, and never a pill: a pill means the session is waiting
        // on the reader, and every session that is has already been lifted into the
        // block above. Two treatments of "needs me" on one page is how the signal
        // stops meaning anything.
        status:
          st.style === 'dot' && st.label && st.colorVar
            ? { colorVar: st.colorVar, label: st.label, detail: st.detail, pulse: st.pulse }
            : undefined,
        // Pushed in recency order — recentSlots is sorted newest-first — which is the
        // order the root's idle-ordered `recent` group keeps, so the row needs no
        // sort key of its own.
        run: async () => {
          // See the attention row above (#15732).
          const switched = dispatch(switchSlot({ key: slot.key, announceOnMissing: true })).unwrap()
          navigate('/chat')
          focusComposerForOpenedSession(switched, slot.key, store)
        },
      })
    })
    rows.push(
      {
        id: 'command:new-session',
        title: i18nT('apps.commandBar.cmd_new_session'),
        group: 'commands',
        kind: 'invoke',
        icon: <MessageSquarePlus size={14} className="lucide-inline" />,
        // `dispatch(...).unwrap()` already returns a promise that rejects on failure,
        // which is the whole contract an `invoke` row needs. Wrapping it in a mutation
        // added state nothing reads, changed identity every render (so this memo never
        // held), and refused to re-run after a rejection -- leaving a failed New
        // Session unretryable without closing the bar.
        //
        // The navigate is part of the action, not decoration: created off-screen from
        // Settings or Task Runner the new session is invisible, so a success reads as a
        // failure and the user runs it again into a duplicate. The palette carried this
        // in the mutation's `onSuccess`; it belongs to the row either way.
        run: async () => {
          await dispatch(createSlot(undefined)).unwrap()
          navigate('/chat')
        },
        keywords: ['chat', 'start', 'blank'],
      },
      {
        id: 'command:toggle-theme',
        title: i18nT('apps.commandBar.cmd_toggle_theme'),
        // The cycle has three stops, so a hop onto `system` that happens to match the
        // current look changes nothing visible and reads as a silent failure. Naming
        // the cycle is what makes that outcome legible. Key already in the catalog.
        subtitle: i18nT('components.commandPalette.providers.actionsProvider.cycle_light_dark_system'),
        group: 'commands',
        kind: 'invoke',
        icon: <SunMoon size={14} className="lucide-inline" />,
        // Same side effect the palette's actions provider invokes, reached through the
        // theme context directly so the row needs nothing threaded into the overlay.
        run: async () => cycleTheme(),
        keywords: ['dark', 'light', 'appearance', 'colour', 'color'],
      },
      {
        id: 'command:search-sessions',
        title: i18nT('apps.commandBar.cmd_search_sessions'),
        group: 'commands',
        kind: 'view',
        view: 'sessions',
        icon: <Search size={14} className="lucide-inline" />,
        keywords: ['history', 'chat', 'conversation'],
      },
      {
        id: 'command:search-artifacts',
        title: i18nT('apps.commandBar.cmd_search_artifacts'),
        // "Artifacts" is this product's word, not the reader's: a first-time reader
        // called them "whatever they are". The subtitle names the things instead of
        // the category, which is what the settings rows in this same list do. The
        // sessions row above needs none, because "Sessions" already says it.
        subtitle: i18nT('apps.commandBar.cmd_search_artifacts_sub'),
        group: 'commands',
        kind: 'view',
        view: 'artifacts',
        // The glyph artifacts carry everywhere else in the product — the side
        // panel's view and the opened-artifact tab both use this one.
        icon: <Component size={14} className="lucide-inline" />,
        // `widget` earns its place: what the user saved is usually an mcwidget, and
        // that is the word they watched the chat call it. The rest are the nouns the
        // artifact list itself is filed under.
        keywords: ['widget', 'html', 'saved', 'document', 'chart'],
      },
      {
        // The sidebar's own folders, reached the way sessions and artifacts are: ONE
        // row that opens a view, not the folder list flattened into the first page.
        // A folder list is a corpus, and the reader has already learned from the two
        // rows above what a corpus costs them here — press Enter, then type.
        // Spreading tens of folder rows through the root instead made the same
        // collection behave unlike every other corpus this surface holds: demoted and
        // capped while the query was empty, so the feature read as missing, and
        // competing with commands once it was not.
        id: 'command:search-folders',
        title: i18nT('apps.commandBar.cmd_search_folders'),
        group: 'commands',
        kind: 'view',
        view: 'folders',
        icon: <Folder size={14} className="lucide-inline" />,
        keywords: ['sidebar', 'tree', 'group'],
      },
    )
    // The crew, reached the way the three corpora above are: ONE row that opens a
    // view. A roster is small enough that flattening it into the first page looks
    // tempting, and `matesProvider` records why it is not — the catalog behind it is a
    // FETCH, so a root group would either issue a request on every open of this bar or
    // show an empty Crewmates group on a cold install.
    //
    // Behind the PREVIEW GATE, because this row is an INGRESS to `/members` and that
    // page is registered with `previewFlag: PREVIEW_CREW`. Every other door to it
    // applies the same gate; a launcher row that did not would advertise a page the
    // operator has not opted into, and this app ships `defaultEnabled: true`, so it
    // would do so on a default install.
    if (crewPreview) {
      rows.push({
        id: 'command:search-mates',
        title: i18nT('apps.commandBar.cmd_search_mates'),
        // "Crewmate" is this product's word, the way "artifact" is: a reader who has
        // not opened the Crewmates page has no reason to know that the agents they
        // talk to have their own durable threads. The subtitle names what the view
        // gets them instead of restating the category.
        subtitle: i18nT('apps.commandBar.cmd_search_mates_sub'),
        group: 'commands',
        kind: 'view',
        view: 'mates',
        icon: matesIcon(),
        // The words a reader who has not learned "crewmate" would reach for. `agent`
        // earns its place above all of them: it is what the rest of the product calls
        // the thing answering them.
        keywords: ['crew', 'agent', 'member', 'mate', 'teammate', 'roster'],
      })
    }
    // Commands contributed by installed apps. This is the seam that lets a row live
    // outside this repository: the app declares the row and what it does, and the
    // host renders and runs it. Nothing app-authored executes here.
    for (const cmd of commandById.values()) {
      rows.push({
        id: cmd.id,
        title: cmd.title,
        // Falls back to the contributing app's name. A contributed row with no
        // subtitle is otherwise indistinguishable from a builtin one, and "which
        // app put this in my launcher" is the first thing a reader asks of a row
        // they did not recognise.
        subtitle: cmd.subtitle || cmd.appLabel,
        group: 'commands',
        kind: 'prompt',
        icon: contributedIcon(cmd.icon),
        keywords: cmd.keywords,
        // Derived, not declared: a command that needs an argument cannot act on an
        // empty query, so it has nothing to offer a launcher that has just opened.
        // Leaving this to the manifest would mean asking every app author to
        // volunteer their row out of the first page, which none would.
        idleDemote: cmd.argument !== null,
        // What the group's cap counts -- see `RootRow.contributed`. This is the one
        // place a row enters the `commands` group from outside the product, so it is
        // the one place the flag is set.
        contributed: true,
        appLabel: cmd.appLabel,
      })
    }
    for (const target of appNavTargets(apps ?? [])) {
      rows.push({
        id: `app:${target.name}`,
        title: target.label,
        group: 'apps',
        kind: 'navigate',
        route: target.route,
        // The app's own art, through the chain the rail and the palette already
        // share. Filed by group alone every app row rendered the same package
        // outline, so the icon column told the reader only that these were apps --
        // which the group header above them and the label to their right both
        // already said.
        icon: appIcon(target),
      })
    }
    for (const entry of SETTINGS_REGISTRY) {
      if (!settingEntryOffered(entry, settingsGovernance)) continue
      rows.push({
        id: `${SETTING_ROW_PREFIX}${entry.id}`,
        // Matched as the other settings searches match it: label, synonyms, then
        // a description or tab that contains the query.
        match: query => settingRowMatch(query, entry),
        // The shared resolver, not a bare labelKey lookup: resolving the key
        // alone drops the fan-out suffix ("Bot Token (Discord)" → "Bot
        // Token"), rendering per-channel rows as indistinguishable titles.
        title: localizedSettingLabel(entry),
        subtitle: settingsSubtitle(entry),
        group: 'settings',
        kind: 'navigate',
        route: settingsRoute(entry),
      })
    }
    return rows
    // `resolved` appears in the deps without appearing in the body on purpose: every
    // title and subtitle above is a catalog lookup, and a language change re-renders
    // the tree without remounting it, which does not recompute a memo. Omitting it
    // would freeze these rows in whichever language the surface first resolved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apps, commandById, crewPreview, cycleTheme, dispatch, liveSlots, navigate, resolved, settingsGovernance, simplifiedToolNames, slotStatusDetail, store, unreadSlots])

  // The root ranks from the LIVE query, not the debounced one. Ranking is pure and
  // local, so there is nothing to throttle, and debouncing it would let a fast Enter
  // -- two keystrokes then return, which is the whole point of a launcher -- activate
  // the row selected against the previous query. Debounce exists for the scoped
  // views, which do hit the network.
  const ranked: RankedRow[] = useMemo(
    () =>
      scope
        ? []
        : rankRootRows(rootRows, query, usage, Date.now(), {
            attentionExpanded,
            attentionOverflowRow: hidden => ({
              id: ATTENTION_MORE_ID,
              title: i18nT('apps.commandBar.attention_more', { count: hidden }),
              group: 'attention',
              kind: 'invoke',
              icon: <ChevronsDown size={14} className="lucide-inline" />,
              expand: () => setAttentionExpanded(true),
            }),
          }),
    [scope, rootRows, query, usage, attentionExpanded, setAttentionExpanded],
  )

  return ranked
}
