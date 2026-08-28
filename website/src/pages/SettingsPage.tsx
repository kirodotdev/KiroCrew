import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Bell, Code, Fingerprint, Globe, History, Import, Info, Keyboard, KeyRound, Link2, MessageSquare, Mic, Palette, PanelsTopLeft, Server, ShieldCheck, Sparkles, SquareMousePointer, Webhook } from 'lucide-react'
import { useAppSelector } from '../store'
import SidePanelLayout from '../components/SidePanelLayout'
import { SUBNAV_PARAM, SUBNAV_LEGACY_PARAMS, deleteSubSelection, toPathSegment, parsePathSegments } from '../components/subNavParams'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { BrowserPanel } from './settings/BrowserPanel'
import { RemoteCrewPanel } from './settings/RemoteCrewPanel'
import { isEmbeddedPane } from '../lib/embedded'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { SkillsPanel } from './settings/SkillsPanel'
import { VoicePanel } from './settings/VoicePanel'
import { DeveloperPanel } from './settings/DeveloperPanel'
import { SecurityPanel } from './settings/SecurityPanel'
import { ChannelsPanel, CHANNEL_KEYS } from './settings/ChannelsPanel'
import { OverviewPanel } from './settings/OverviewPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'
import { ShortcutsPanel } from './settings/ShortcutsPanel'
import { AboutPanel } from './settings/AboutPanel'
import ReleasesPanel from './settings/ReleasesPanel'
import { ImportPanel } from './settings/ImportPanel'
import { ComputerUsePanel } from './settings/ComputerUsePanel'
import { WebhooksPanel } from './settings/WebhooksPanel'
import { PrivacyPanel } from './settings/PrivacyPanel'
import { SecretsPanel } from './settings/SecretsPanel'
import SettingsSearch from './settings/SettingsSearch'

import { i18nT } from '../i18n/t'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_WEBHOOKS } from '../utils/previewFlags'
// Group headers double as the grouping KEY (SidePanelLayout starts a new header
// whenever this string changes), so they are resolved inside `buildTabs()` —
// which runs per render — rather than at module load. Translating them at module
// scope would freeze the header in the boot language while the tabs switched.

/**
 * Settings tab descriptors.
 *
 * A FUNCTION, not a module-level array: the labels/descriptions are translated,
 * and a module-level constant would freeze them in whichever language was active
 * at import time — leaving the tab rail English after a language switch. Called
 * once per render inside the component instead.
 *
 * The group headers double as the grouping KEY (SidePanelLayout starts a new
 * header whenever this string changes), so they are resolved here too.
 */
function buildTabs() {
  const GROUP_PREFERENCES = i18nT('settings.groups.preferences')
  const GROUP_SYSTEM = i18nT('settings.groups.system')
  return [
    { key: 'overview', label: i18nT('settings.tabs.overview.label'), icon: <PanelsTopLeft size={16} />, description: i18nT('settings.tabs.overview.description') },
    { key: 'imports', label: i18nT('settings.tabs.imports.label'), icon: <Import size={16} />, description: i18nT('settings.tabs.imports.description') },
    { key: 'chat', label: i18nT('settings.tabs.chat.label'), icon: <MessageSquare size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.chat.description') },
    { key: 'display', label: i18nT('settings.tabs.display.label'), icon: <Palette size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.display.description') },
    { key: 'voice', label: i18nT('settings.tabs.voice.label'), icon: <Mic size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.voice.description') },
    { key: 'notifications', label: i18nT('settings.tabs.notifications.label'), icon: <Bell size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.notifications.description') },
    { key: 'shortcuts', label: i18nT('settings.tabs.shortcuts.label'), icon: <Keyboard size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.shortcuts.description') },
    { key: 'skills', label: i18nT('settings.tabs.skills.label'), icon: <Sparkles size={16} />, group: GROUP_PREFERENCES, description: i18nT('settings.tabs.skills.description') },
    { key: 'channels', label: i18nT('settings.tabs.channels.label'), icon: <Link2 size={16} />, description: i18nT('settings.tabs.channels.description'), hostsSubNav: true },
    { key: 'browser', label: i18nT('settings.tabs.browser.label'), icon: <Globe size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.browser.description') },
    { key: 'computer-use', label: i18nT('settings.tabs.computerUse.label'), icon: <SquareMousePointer className="lucide-inline" />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.computerUse.description') },
    { key: 'webhooks', label: i18nT('settings.tabs.webhooks.label'), icon: <Webhook size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.webhooks.description') },
    { key: 'instances', label: i18nT('settings.tabs.instances.label'), icon: <Server size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.instances.description') },
    { key: 'privacy', label: i18nT('privacyDisclosure.settingsLabel'), icon: <Fingerprint className="lucide-inline" />, group: GROUP_SYSTEM, description: i18nT('privacyDisclosure.settingsDescription') },
    { key: 'security', label: i18nT('settings.tabs.security.label'), icon: <ShieldCheck size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.security.description'), hostsSubNav: true },
    { key: 'secrets', label: i18nT('settings.tabs.secrets.label'), icon: <KeyRound size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.secrets.description') },
    { key: 'developer', label: i18nT('settings.tabs.developer.label'), icon: <Code size={16} />, group: GROUP_SYSTEM, description: i18nT('settings.tabs.developer.description') },
    // The trailing divider fences off the entries that are not settings at all.
    // About was its only occupant; the release archive is the same kind of thing
    // (a document about the product, not a preference), so it joins the fence
    // rather than opening a second exception. The divider flag rides the FIRST
    // entry after it -- `SidePanelLayout` renders one separator per flagged row,
    // so leaving it on About too would draw two lines and strand this row
    // outside the fence. About stays last, where every platform puts it.
    // The one Settings tab that is contained rather than page-scrolled: it has
    // its own version rail beside the notes, and letting the page grow scrolled
    // the rail and the "Releases" heading off the top while the reader was
    // still inside one release's notes.
    { key: 'releases', label: i18nT('settings.tabs.releases.label'), icon: <History size={16} />, dividerBefore: true, fixedContent: true, description: i18nT('settings.tabs.releases.description') },
    { key: 'about', label: i18nT('settings.tabs.about.label'), icon: <Info size={16} />, description: i18nT('settings.tabs.about.description') },
  ]
}

/** Base of the Settings path tree. The route is a `/settings/*` splat and the
 *  trailing segments are the navigation state: segment[0] = tab,
 *  segment[1] = a SubNav's second-level selection, deeper segments reserved
 *  (rendered as if absent). Parsing lives in the basePath seam
 *  (SidePanelLayout reads segment[0], SettingsSubNav segment[1]); this
 *  constant is the single spelling handed to both so they cannot drift.
 *  The mobile root list is the bare base with no segments. */
const SETTINGS_BASE_PATH = '/settings'

export default function SettingsPage() {
  // Folded for display on the stable channel (`version_display`), raw
  // `version` fallback for a gateway that predates the field. Display-only:
  // nothing here compares or arms on it.
  const version = useAppSelector(s => s.dashboard.status?.version_display || s.dashboard.status?.version) || '—'
  useSettingHighlight()
  const navigate = useNavigate()
  const location = useLocation()

  // Legacy query-param translation: Settings navigation used to live in query
  // params (?tab=X&sub=Y, with the per-host `channel`/`section` aliases, and
  // before the nav regroup five per-channel tabs — ?tab=slack). All of those
  // URLs survive in bookmarks, command-palette history and docs, so any legacy
  // navigation param in the CURRENT location — at mount and on every later
  // in-place navigation (palette history, in-app legacy links) — is translated
  // into the canonical path form
  // (/settings/<tab>/<sub>) with a REPLACE navigation — back must never land on
  // the pre-translation URL. Deliberately NOT a one-shot: a mount-only
  // translation would leave permanent hybrid URLs for legacy links followed
  // while Settings is already open. Non-navigation params (`highlight`,
  // anything else) ride along untouched. Plain useEffect on purpose: react-router 7 drops
  // navigations fired from useLayoutEffect during the initial mount (its ready
  // flag is set in a passive effect), so the translation must run as a passive
  // effect too. Until it fires, SidePanelLayout treats the segment-less path as
  // the default tab for one frame.
  const search = location.search
  const pathname = location.pathname
  useEffect(() => {
    const qs = new URLSearchParams(search)
    // Fire on legacy-param PRESENCE, not value: even a degenerate `?tab=`
    // must be consumed (stripped with a replace) or it lingers as a permanent
    // hybrid URL. Value handling below decides what it translates TO.
    if (!['tab', SUBNAV_PARAM, ...SUBNAV_LEGACY_PARAMS].some(p => qs.has(p))) return
    // An empty value (`?tab=`) or a dot-only value (`?tab=..` — which URL
    // normalization would resolve outside /settings, and no encoding makes
    // safe) is junk, not navigation intent: treat it as ABSENT so the
    // segment backfill below preserves the path state the link arrived on,
    // while the navigate still strips the param itself.
    let tab = qs.get('tab') || null
    // Canonical `sub` wins BY PRESENCE over the historical per-host aliases —
    // the same precedence SettingsSubNav applies on its read path. A present
    // `?sub=` (even empty) therefore silences the aliases; its empty value
    // then normalizes to "absent" like every other degenerate value, letting
    // the segment backfill below preserve the path selection.
    let sub = qs.has(SUBNAV_PARAM)
      ? qs.get(SUBNAV_PARAM) || null
      : SUBNAV_LEGACY_PARAMS.map(p => qs.get(p)).find(v => v != null) || null
    if (tab === '.' || tab === '..') tab = null
    if (sub === '.' || sub === '..') sub = null
    // Oldest form: the five per-channel tabs collapsed into one Channels tab,
    // so ?tab=slack means the slack pane INSIDE channels. This remap
    // deliberately OVERWRITES any explicit ?sub= (byte-for-byte the historical
    // behavior) — no real writer ever minted the pair, since the per-channel
    // tabs predate the sub level entirely.
    if (tab && CHANNEL_KEYS.includes(tab)) {
      sub = tab
      tab = 'channels'
    }
    // A hand-crafted hybrid can carry path segments AND legacy params. The
    // query expresses the link author's intent, so it wins where it speaks;
    // existing segments fill the gaps — but a segment sub only attaches to
    // its own tab (inheriting it across a query-supplied tab change would
    // strand a selection in a pane that doesn't host it).
    const segs = parsePathSegments(SETTINGS_BASE_PATH, pathname)
    if (tab == null) tab = segs[0] || null
    if (sub == null && tab != null && tab === segs[0]) sub = segs[1] || null
    qs.delete('tab')
    deleteSubSelection(qs)
    const rest = qs.toString()
    // A second-level selection is scoped to the tab that hosts it — an alias
    // arriving without any tab has nothing to attach to and is dropped,
    // exactly what the query model did (aliases were only read by a mounted
    // panel). toPathSegment encodes each value as exactly one segment and
    // rejects dot-only values (`?tab=..` must not resolve outside /settings),
    // so a crafted param can neither mint fake depth nor escape the tree.
    const tabSeg = tab != null ? toPathSegment(tab) : null
    const subSeg = sub != null ? toPathSegment(sub) : null
    const target = tabSeg
      ? subSeg
        ? `${SETTINGS_BASE_PATH}/${tabSeg}/${subSeg}`
        : `${SETTINGS_BASE_PATH}/${tabSeg}`
      : SETTINGS_BASE_PATH
    navigate({ pathname: target, search: rest ? `?${rest}` : '' }, { replace: true })
  }, [search, pathname, navigate])

  // An embedded instance pane can't manage remote instances (single-level by
  // design) — hide the Instances tab so a pane can't connect onward.
  const embedded = isEmbeddedPane()
  // Update nudge: dot on the About entry while an update is available. Two
  // independent sources, because they cover different installs: the Electron
  // updater's mirrored flag (desktop only) and the gateway's own verdict (every
  // other shape). Keying on the desktop flag alone left the dot permanently dark
  // on a wheel install, which is the majority of installs.
  //
  // `=== true` is required, not cosmetic: the gateway sends null for a check that
  // never ran or failed, and a truthiness test would keep that dark while a
  // `!== false` test would light it on no evidence.
  const gatewayUpdateAvailable = useAppSelector(s => s.dashboard.status?.update_available)
  const desktopUpdateAvailable = useAppSelector(s => s.dashboard.desktopUpdateAvailable)
  const updateAvailable = gatewayUpdateAvailable === true || desktopUpdateAvailable
  // Inbound webhooks is preview-gated. The rail and the palette apply that gate
  // through `getAdvertisedSurfaces()`, but this tab is the surface's only
  // advertised home (it is `hiddenFromNav`), so the gate has to be applied here
  // or an unreleased page would be listed for everyone. The hook, rather than a
  // bare `readPreviewFlag`, so toggling it in Developer > Feature Previews updates this
  // rail without a reload.
  const webhooksPreview = usePreviewFlag(PREVIEW_WEBHOOKS)
  const allTabs = buildTabs().filter(t => t.key !== 'webhooks' || webhooksPreview)
  const baseTabs = embedded ? allTabs.filter(t => t.key !== 'instances') : allTabs
  const tabs = updateAvailable ? baseTabs.map(t => (t.key === 'about' ? { ...t, dot: true } : t)) : baseTabs

  return (
    <SidePanelLayout
      title={i18nT('pages.settingsPage.settings')}
      tabs={tabs}
      basePath={SETTINGS_BASE_PATH}
      headerRightDock="bottom-float"
      // Keyed apart from the main window: an embedded pane has a different tab
      // roster (no Instances), so the two must not restore each other's tab.
      rememberKey={embedded ? 'settings-embedded' : 'settings'}
      headerRight={<SettingsSearch />}
      footer={<span className="text-[12px] text-muted">{i18nT('pages.settingsPage.kirocrew_v')}{version}</span>}
    >
      {tab => <>
        {tab === 'overview' && <OverviewPanel />}
        {tab === 'imports' && <ImportPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'display' && <DisplayPanel />}
        {tab === 'voice' && <VoicePanel />}
        {tab === 'notifications' && <NotificationsPanel />}
        {tab === 'shortcuts' && <ShortcutsPanel />}
        {tab === 'skills' && <SkillsPanel />}
        {tab === 'channels' && <ChannelsPanel basePath={SETTINGS_BASE_PATH} />}
        {tab === 'browser' && <BrowserPanel />}
        {tab === 'computer-use' && <ComputerUsePanel />}
        {tab === 'webhooks' && <WebhooksPanel />}
        {tab === 'instances' && !embedded && <RemoteCrewPanel />}
        {tab === 'privacy' && <PrivacyPanel />}
        {tab === 'security' && <SecurityPanel basePath={SETTINGS_BASE_PATH} />}
        {tab === 'secrets' && <SecretsPanel />}
        {tab === 'developer' && <DeveloperPanel />}
        {tab === 'releases' && <ReleasesPanel />}
        {tab === 'about' && <AboutPanel />}
      </>}
    </SidePanelLayout>
  )
}
