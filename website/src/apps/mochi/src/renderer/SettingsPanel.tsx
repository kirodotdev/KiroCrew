/**
 * Mochi - Settings panel
 * All changes are staged locally. Only applied on Save.
 * Closing without saving prompts the user and reverts if discarded.
 */
import React, { useEffect, useState } from 'react'
import type { AppConfig } from '../shared/config'
import { ELECTRON_MAP, MODIFIER_GLYPHS, MODIFIER_ORDER } from '../shared/config'
import type { CompanionStats, McpServerInfo } from '../shared/types'
import {
  AlertTriangle,
  Bell,
  BookOpen,
  Bot,
  Brain,
  Camera,
  Cat,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  EyeOff,
  Flame,
  Info,
  Keyboard,
  Lock,
  MessageCircle,
  MonitorSmartphone,
  Moon,
  MousePointer,
  Palette,
  PawPrint,
  Plug,
  Plus,
  Settings,
  Smile,
  Sun,
  type LucideIcon,
  X,
  Zap,
} from 'lucide-react'
import { applyTheme, type ThemeId } from '../shared/themes'
import { MochiInstancesList } from '../../panel/MochiInstances'
import { formatThinkingTime, getTopMoods, shouldShowStat, formatDate, formatCompanionTime } from '../shared/statsFormatters'

import { api } from '../mochiApi'
import { i18nT } from '../../../../i18n/t'
import { SUPPORTED_LANGUAGES } from '../../../../i18n/languages'
import { moodLabel } from '../../i18nKeys'
import { fmtNumber, fmtPercent } from '../../../../i18n/format'

type SettingsSectionId =
  | 'general' | 'memories' | 'appearance' | 'behavior' | 'notifications'
  | 'background' | 'model' | 'mcp' | 'trust' | 'shortcuts' | 'instances' | 'about'

/**
 * Left-rail labels, keyed by section id.
 *
 * A separate map rather than a `labelKey` field on the row below, because
 * `check-i18n-keys.mjs` can only verify a key it can resolve syntactically: a key read
 * off a mapped array element is opaque to it, so the whole file would be exempt from
 * the catalog-existence check. Indexing this map at the render site resolves to the
 * union of its values, which checks all twelve.
 */
const SECTION_LABEL_KEY: Record<SettingsSectionId, string> = {
  general: 'apps.mochi.settingsPanel.general',
  memories: 'apps.mochi.stats.title',
  appearance: 'apps.mochi.settingsPanel.appearance',
  behavior: 'apps.mochi.settingsPanel.behavior',
  notifications: 'apps.mochi.settingsPanel.notifications',
  background: 'apps.mochi.settingsPanel.bg_activity',
  model: 'apps.mochi.settingsPanel.model',
  mcp: 'apps.mochi.settingsPanel.mcp_title',
  trust: 'apps.mochi.settingsPanel.trust',
  shortcuts: 'apps.mochi.settingsPanel.shortcuts',
  instances: 'apps.mochi.settingsPanel.instances',
  about: 'apps.mochi.settingsPanel.about',
} as const

/** Left-rail order and icons. */
const SETTINGS_SECTIONS: {
  id: SettingsSectionId
  Icon: LucideIcon
}[] = [
  { id: 'general', Icon: Settings },
  { id: 'memories', Icon: Brain },
  { id: 'appearance', Icon: Palette },
  { id: 'behavior', Icon: PawPrint },
  { id: 'notifications', Icon: Bell },
  { id: 'background', Icon: Zap },
  { id: 'model', Icon: Bot },
  { id: 'mcp', Icon: Plug },
  { id: 'trust', Icon: Lock },
  { id: 'shortcuts', Icon: Keyboard },
  { id: 'instances', Icon: MonitorSmartphone },
  { id: 'about', Icon: Info },
]

export const SettingsPanel: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const [original, setOriginal] = useState<AppConfig | null>(null)
  const [config, setConfig] = useState<AppConfig | null>(null)
  // Two-column layout: the left rail selects which section the right pane
  // shows. The old single scroll column meant hunting for a control by
  // scrolling past every unrelated one.
  const [active, setActive] = useState<SettingsSectionId>('general')
  const [shortcutRefused, setShortcutRefused] = useState<string[]>([])
  // A switch the shell refused, or one attempted with no shell at all. Kept out
  // of `shortcutRefused` so the two failures can say different things -- clearing
  // dirty state on a refused switch is what made the panel read "saved" while the
  // pet never moved.
  const [instanceSwitchFailed, setInstanceSwitchFailed] = useState(false)
  const [trustMode, setTrustMode] = useState<'normal' | 'trust_reads' | 'trust' | 'yolo'>('normal')
  const [origTrust, setOrigTrust] = useState<'normal' | 'trust_reads' | 'trust' | 'yolo'>('normal')
  const [showUnsaved, setShowUnsaved] = useState(false)
  const [stats, setStats] = useState<CompanionStats | null>(null)
  const isDirty = (() => {
    if (!config || !original) return false
    if (trustMode !== origTrust) return true
    return JSON.stringify(config) !== JSON.stringify(original)
  })()

  // Listen for native window close (red × button)
  useEffect(() => {
    const off = api?.onSettingsCloseRequested?.(() => handleClose())
    return () => { off?.() }
  })

  useEffect(() => {
    api?.getConfig?.().then((c: AppConfig) => {
      setOriginal(JSON.parse(JSON.stringify(c)))
      setConfig(c)
    })
    // Trust is SLOT state, not app config. Upstream stored it in its own config
    // and its main process pushed it into the session; as a builtin the slot is
    // the single source of truth, so read and write it directly.
    api?.getMochiTrustLevel?.().then((level) => {
      setTrustMode(level)
      setOrigTrust(level)
    })
    api?.getStats?.().then((s: CompanionStats) => setStats(s))
  }, [])

  useEffect(() => {
    // Stats keep MOVING while this window is open and not being looked at — the
    // pet walks, gets messages, takes screenshots. A mount-only read left the
    // panel showing whatever the counters said when it opened, which is exactly
    // when a "how much has it done" number is least believable.
    //
    // Refetched on focus rather than polled: the numbers only matter when the
    // user is reading them, and a timer in a settings window is a cost paid
    // whether or not anyone is looking. Plain `.then`, not React Query — Mochi's
    // Electron windows mount with a bare `createRoot` and no QueryClientProvider,
    // so `useQuery` here throws "No QueryClient set".
    let alive = true
    const load = () => {
      void api?.getStats?.().then((s: CompanionStats) => { if (alive) setStats(s) })
    }
    window.addEventListener('focus', load)
    return () => { alive = false; window.removeEventListener('focus', load) }
  }, [])

  if (!config || !original) return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', background: 'var(--bg)',
    }}>
      <div style={{
        width: 24, height: 24, border: '2.5px solid var(--border)',
        borderTopColor: 'var(--accent)', borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
      }} />
    </div>
  )

  const edit = (partial: Partial<AppConfig>) => {
    setConfig(prev => prev ? { ...prev, ...partial } : prev)
  }
  const editMochi = (partial: Partial<AppConfig['mochi']>) => {
    setConfig(prev => prev ? { ...prev, mochi: { ...prev.mochi, ...partial } } : prev)
  }

  const handleSave = async () => {
    const finalConfig = { ...config }
    // Slot-scoped: this must never move the dashboard's global approval mode.
    if (trustMode !== origTrust) await api?.setMochiTrustLevel?.(trustMode)
    // AWAIT before closing. Upstream could fire-and-forget because its
    // updateConfig went over IPC and the MAIN process owned the write; here the
    // seam does a fetch FROM THIS WINDOW, so closing it cancelled the in-flight
    // request and the save was silently lost.
    await api?.updateConfig?.(finalConfig)
    // SHORTCUTS BEFORE THE INSTANCE SWITCH, and that order is load-bearing.
    // `setPetInstance` reconciles inline, and a reconcile that sees a changed
    // target calls closeSettingsWindow() — destroying THIS window while
    // handleSave is still awaiting, so anything after it never runs. Since the
    // shell's store is now the only place the accelerators are persisted (they
    // are stripped from the settings POST above), running this second would lose
    // the whole shortcut change, not merely its immediate binding.
    //
    // Bind NOW and report a refusal: globalShortcut.register is the only place
    // "is this key free?" can be answered, and without this call a taken key is
    // silently shown as bound, which reads as "the shortcut just doesn't work".
    const bindResult = (await api?.applyShortcuts?.(config.shortcuts)) ?? {}
    const refused = Object.entries(bindResult)
      .filter(([, ok]) => ok === false)
      .map(([action]) => action)
    if (refused.length > 0) {
      setShortcutRefused(refused)
      // Reveal the section that RENDERS the message. Save is global but this
      // refusal only renders under 'shortcuts', so saving from any other
      // section would hold the panel open with no visible reason for it.
      setActive('shortcuts')
      return // keep the panel open so the message is seen
    }
    setShortcutRefused([])

    // The shell only notices petInstance on its next reconcile pass, so without
    // an explicit apply the pet keeps showing the old instance for a few seconds
    // after the user picked a new one — long enough to read as "the switch didn't
    // work" and invite a second click.
    if (config.mochi.petInstance !== original?.mochi?.petInstance) {
      // Through the SHELL, not the same-origin settings POST. The pointer is a
      // per-MACHINE choice and the shell's store owns it; posting it here would
      // write it onto whichever gateway served this window, where nothing reads
      // it — the one-way door that stranded a pet on a remote with no way back.
      // One call stores AND moves, so the two cannot half-happen.
      //
      // The result is CHECKED: a refused or shell-less switch must not clear the
      // dirty state and read as "saved" while the pet never moved.
      const moved = api?.setPetInstance
        ? await api.setPetInstance(config.mochi.petInstance as string)
        : false
      if (!moved) {
        setInstanceSwitchFailed(true)
        // Same reason as the shortcut refusal above: this message lives under
        // 'instances', and a save started from elsewhere must not strand the
        // user in a panel that refuses to close and does not say why.
        setActive('instances')
        // COMMIT THE BASELINE even though the switch did not land. The shell
        // stores the pointer BEFORE the reconcile that failed, so the choice is
        // already persisted and its retry loop will apply it — there is nothing
        // here to revert. Leaving the old baseline in place would offer a Discard
        // that resets these fields on screen while the pet still moves minutes
        // later, which is the one thing worse than reporting the failure.
        //
        // Everything else on this path has landed too: trust, `updateConfig` and
        // the accelerators all ran before this point. The refused-shortcut branch
        // above deliberately does NOT do this — a refused key really was not
        // stored, so it must stay dirty for the user to fix, and no watcher will
        // apply it behind their back.
        setOriginal(JSON.parse(JSON.stringify(config)))
        setOrigTrust(trustMode)
        return // keep the panel open, same as a refused shortcut
      }
      setInstanceSwitchFailed(false)
    }
    setOriginal(JSON.parse(JSON.stringify(config)))
    setOrigTrust(trustMode)
    onClose()
  }

  const handleDiscard = () => {
    setConfig(JSON.parse(JSON.stringify(original)))
    setTrustMode(origTrust)
    if (original.mochi?.theme) {
      applyTheme(original.mochi.theme as ThemeId)
    }
    onClose()
  }

  const handleClose = () => {
    if (isDirty) { setShowUnsaved(true) } else { onClose() }
  }

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100%',
      color: 'var(--text)', background: 'var(--bg)', position: 'relative',
    }}>
      {/* Header stays put so the close button never scrolls away. */}
      <div style={{
        display: 'flex', alignItems: 'center', padding: '12px 16px',
        borderBottom: '1px solid var(--border)', flexShrink: 0,
      }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 15, fontWeight: 600 }}><Settings size={15} /> {i18nT('apps.mochi.settingsPanel.title')}</span>
        <button onClick={handleClose} aria-label={i18nT('apps.mochi.watchPanel.close')} style={{
          marginLeft: 'auto', background: 'none', border: 'none',
          color: 'var(--text-muted)', cursor: 'pointer', fontSize: 16,
          display: 'flex', alignItems: 'center',
        }}><X size={16} /></button>
      </div>

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* Left rail: section picker. */}
        <nav style={{
          width: 148, flexShrink: 0, overflowY: 'auto', padding: '8px 6px',
          borderRight: '1px solid var(--border)', display: 'flex',
          flexDirection: 'column', gap: 2,
        }}>
          {SETTINGS_SECTIONS.map((s) => {
            const on = active === s.id
            return (
              <button key={s.id} onClick={() => setActive(s.id)} style={{
                display: 'flex', alignItems: 'center', gap: 7, textAlign: 'left',
                padding: '6px 8px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                border: '1px solid transparent',
                background: on ? 'var(--accent-glow)' : 'transparent',
                borderColor: on ? 'var(--accent)' : 'transparent',
                color: on ? 'var(--text)' : 'var(--text-muted)',
                fontWeight: on ? 600 : 400,
                transition: 'background 120ms ease',
              }}>
                <s.Icon size={13} strokeWidth={2} aria-hidden style={{ flexShrink: 0 }} />
                <span style={{ lineHeight: 1.25 }}>{i18nT(SECTION_LABEL_KEY[s.id])}</span>
              </button>
            )
          })}
        </nav>

        {/* Right pane: only the selected section. */}
        {/* minWidth is a FLOOR, not 0: a squeezed content column clipped labels
            mid-word. Below it the pane scrolls horizontally instead. */}
        <div style={{ flex: 1, minWidth: 280, overflowY: 'auto', overflowX: 'auto', padding: 16 }}>

      {active === 'general' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.general')}>
        <Field label={i18nT('apps.mochi.settingsPanel.pet_name')} desc={i18nT('apps.mochi.settingsPanel.pet_name_desc')} value={config.mochi.petName}
          onChange={(v) => editMochi({ petName: v })} />
        {/* Upstream stored the full words 'Chinese' / 'English'. This build stores
            '' (follow the system) | 'en' | 'zh', so those options matched NOTHING
            and the browser fell back to showing the FIRST one — the box read
            "Chinese" while the stored value was actually "follow the system". */}
        <SelectField label={i18nT('apps.mochi.settingsPanel.language')} desc={i18nT('apps.mochi.settingsPanel.language_desc', { name: config.mochi.petName || 'Mochi' })}
          value={config.mochi.language}
          options={[
            // "Auto" is not a language — it is "follow KiroCrew", which is what an
            // empty value means all the way down to initI18n(). The rest come from
            // the core registry rather than being listed here, so a language added
            // to KiroCrew appears for Mochi too instead of silently going missing.
            { value: '', label: i18nT('apps.mochi.settingsPanel.language_auto') },
            ...SUPPORTED_LANGUAGES.filter(l => !l.devOnly).map(l => ({ value: l.code, label: l.label })),
          ]}
          onChange={(v) => editMochi({ language: v })} />
      </Section>
      </>)}
      {active === 'memories' && (<>
      <MemoriesSection stats={stats} petName={config.mochi.petName} />
      </>)}
      {active === 'appearance' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.appearance')}>
        <button onClick={() => api?.galleryOpen?.()} style={{
          width: '100%', padding: '6px 0', borderRadius: 6, fontSize: 12, cursor: 'pointer',
          border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text)',
        }}>{i18nT('apps.mochi.settingsPanel.gallery')}</button>
      </Section>
      </>)}
      {active === 'behavior' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.behavior')}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {([
            { value: 'active' as const, Icon: Zap, label: i18nT('apps.mochi.settingsPanel.mode_active'), desc: i18nT('apps.mochi.settingsPanel.mode_active_desc') },
            { value: 'normal' as const, Icon: Cat, label: i18nT('apps.mochi.settingsPanel.mode_normal'), desc: i18nT('apps.mochi.settingsPanel.mode_normal_desc') },
            { value: 'quiet' as const, Icon: Moon, label: i18nT('apps.mochi.settingsPanel.mode_quiet'), desc: i18nT('apps.mochi.settingsPanel.mode_quiet_desc') },
          ]).map((opt) => (
            <label key={opt.value} onClick={() => editMochi({ activityMode: opt.value })} style={{
              display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer',
              padding: '6px 8px', borderRadius: 6,
              background: config.mochi.activityMode === opt.value ? 'var(--accent-glow)' : 'transparent',
              border: config.mochi.activityMode === opt.value ? '1px solid var(--accent)' : '1px solid transparent',
            }}>
              <div style={{
                width: 14, height: 14, borderRadius: '50%', flexShrink: 0, marginTop: 1,
                border: config.mochi.activityMode === opt.value ? '4px solid var(--accent)' : '2px solid var(--border)',
                background: 'var(--bg)',
              }} />
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text)', fontWeight: config.mochi.activityMode === opt.value ? 600 : 400 }}><opt.Icon size={13} /> {opt.label}</div>
                <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{opt.desc}</div>
              </div>
            </label>
          ))}
        </div>
      </Section>
      </>)}
      {active === 'notifications' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.notifications')}>
        <Toggle label={i18nT('apps.mochi.settingsPanel.silent_subagents')} desc={i18nT('apps.mochi.settingsPanel.silent_subagents_desc')}
          value={config.mochi.silentSubagents ?? false}
          onChange={(v) => editMochi({ silentSubagents: v })} />
        <Toggle label={i18nT('apps.mochi.settingsPanel.chat_always_on_top')} desc={i18nT('apps.mochi.settingsPanel.chat_always_on_top_desc')}
          value={config.window.chatAlwaysOnTop}
          onChange={(v) => edit({ window: { ...config.window, chatAlwaysOnTop: v } })} />
      </Section>
      </>)}
      {active === 'background' && (<>
      <BackgroundActivitySection config={config} editMochi={editMochi} />
      </>)}
      {active === 'model' && (<>
      <ModelSelector config={config} editMochi={editMochi} />
      </>)}
      {active === 'mcp' && (<>
      <McpSection config={config} editMochi={editMochi} trustMode={trustMode} />
      </>)}
      {active === 'trust' && (<>
      {/* Trust level for MOCHI'S SLOT ONLY -- the dashboard's global approval
          mode is untouched. Restored from upstream; the difference is that the
          value is read from and written to the slot, not to app config. */}
      <Section title={i18nT('apps.mochi.settingsPanel.trust')}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {/* `yolo` is deliberately NOT offered here. The level is app-wide (it
              lives on the slot, not in Mochi's config), and the dashboard's own
              picker gates that choice behind an explicit confirm. A plain radio
              + Save in a pet settings window would flip approvals off for every
              surface while skipping that gate — a weaker path to a stronger
              permission. Someone who wants it sets it where the confirmation
              lives; Mochi still READS and reports the level (see McpSection), so
              a slot already in yolo is shown truthfully rather than mislabelled. */}
          {([
            { value: 'normal', label: i18nT('apps.mochi.settingsPanel.trust_normal'), desc: i18nT('apps.mochi.settingsPanel.trust_normal_desc') },
            { value: 'trust_reads', label: i18nT('apps.mochi.settingsPanel.trust_reads'), desc: i18nT('apps.mochi.settingsPanel.trust_reads_desc') },
            { value: 'trust', label: i18nT('apps.mochi.settingsPanel.trust_trust'), desc: i18nT('apps.mochi.settingsPanel.trust_trust_desc') },
          ] as const).map((opt) => (
            <label key={opt.value} onClick={() => setTrustMode(opt.value)} style={{
              display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer',
              padding: '6px 8px', borderRadius: 6,
              background: trustMode === opt.value ? 'var(--accent-glow)' : 'transparent',
              border: trustMode === opt.value ? '1px solid var(--accent)' : '1px solid transparent',
            }}>
              <div style={{
                width: 14, height: 14, borderRadius: '50%', flexShrink: 0, marginTop: 1,
                border: trustMode === opt.value ? '4px solid var(--accent)' : '2px solid var(--border)',
                background: 'var(--bg)',
              }} />
              <div>
                <div style={{ fontSize: 12, color: 'var(--text)', fontWeight: trustMode === opt.value ? 600 : 400 }}>{opt.label}</div>
                <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{opt.desc}</div>
              </div>
            </label>
          ))}
          {/* The level is app-wide and `yolo` is not settable here, so a slot
              already in it would otherwise render as three unselected radios —
              which reads as "no level set" rather than "the strongest one".
              Say so, and name where it can be changed. */}
          {trustMode === 'yolo' && (
            <div style={{
              fontSize: 10, color: '#ca8a04', padding: '6px 8px', borderRadius: 6,
              background: 'rgba(234,179,8,0.1)', border: '1px solid rgba(234,179,8,0.3)',
            }}>
              {i18nT('apps.mochi.settingsPanel.trust_yolo_active')}
            </div>
          )}
        </div>
      </Section>
      </>)}
      {active === 'instances' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.instances')}>
        {/* No shell means no IPC, and the pointer lives in the shell's store --
            so picking a row here could store nothing and move nothing. Say that
            instead of rendering a control that silently does nothing. */}
        {!api.hasShell ? (
          <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6 }}>
            {i18nT('apps.mochi.instances.desktop_only')}
          </div>
        ) : (<>
          {instanceSwitchFailed && (
            <div role="alert" style={{
              fontSize: 10, color: 'var(--danger)', marginBottom: 6, lineHeight: 1.4,
            }}>
              {i18nT('apps.mochi.instances.switch_failed')}
            </div>
          )}
          <MochiInstancesList
            value={(config.mochi as { petInstance?: string }).petInstance || 'self'}
            onChange={(petInstance) => editMochi({ petInstance })}
          />
        </>)}
      </Section>
      </>)}

      {active === 'about' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.about')}>
        <div style={{ fontSize: 12, color: 'var(--text)', marginBottom: 6 }}>
          {i18nT('apps.mochi.settingsPanel.about_blurb', { name: config.mochi.petName || 'Mochi' })}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.7 }}>
          {i18nT('apps.mochi.settingsPanel.about_author')}:{' '}
          <span
            role="link"
            tabIndex={0}
            onClick={() => api?.openExternal?.('https://github.com/buluoray')}
            onKeyDown={(e) => { if (e.key === 'Enter') api?.openExternal?.('https://github.com/buluoray') }}
            style={{ color: 'var(--accent)', cursor: 'pointer' }}
            onMouseEnter={(e) => (e.currentTarget.style.textDecoration = 'underline')}
            onMouseLeave={(e) => (e.currentTarget.style.textDecoration = 'none')}
          >{'buluoray'}</span>
        </div>
      </Section>
      </>)}

      {active === 'shortcuts' && (<>
      <Section title={i18nT('apps.mochi.settingsPanel.shortcuts')}>
        {/* No shell means no way to register an OS global accelerator AND no
            store to persist one in: the shell's store is the only copy, and
            `flattenConfig` deliberately does not post these to the gateway. So
            without it, editing here would report success and change nothing.
            Same call as the instances pane above — say so rather than render a
            control that cannot deliver. */}
        {!api.hasShell ? (
          <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6 }}>
            {i18nT('apps.mochi.settingsPanel.shortcuts_desktop_only')}
          </div>
        ) : (<>
        {shortcutRefused.length > 0 && (
          <div role="alert" style={{
            fontSize: 10, color: 'var(--danger)', marginBottom: 6, lineHeight: 1.4,
          }}>
            {i18nT('apps.mochi.settingsPanel.shortcut_taken', { actions: shortcutRefused.join(', ') })}
          </div>
        )}
        <ShortcutField label={i18nT('apps.mochi.settingsPanel.screen_capture')} desc={i18nT('apps.mochi.settingsPanel.screen_capture_desc')}
          value={config.shortcuts.screenCapture}
          onChange={(v) => edit({ shortcuts: { ...config.shortcuts, screenCapture: v } })} />
        <ShortcutField label={i18nT('apps.mochi.settingsPanel.toggle_window')} desc={i18nT('apps.mochi.settingsPanel.toggle_window_desc')}
          value={config.shortcuts.toggleWindow}
          onChange={(v) => edit({ shortcuts: { ...config.shortcuts, toggleWindow: v } })} />
        <ShortcutField label={i18nT('apps.mochi.settingsPanel.hide_all')} desc={i18nT('apps.mochi.settingsPanel.hide_all_desc')}
          value={config.shortcuts.hideAll}
          onChange={(v) => edit({ shortcuts: { ...config.shortcuts, hideAll: v } })} />
        </>)}
      </Section>
      </>)}

        </div>{/* right pane */}
      </div>{/* body row */}

      {/* Footer is OUTSIDE the scroll area: Save must be reachable from every
          section without scrolling to the bottom of that section. */}
      <div style={{
        flexShrink: 0, padding: '10px 16px', borderTop: '1px solid var(--border)',
        background: 'var(--bg)',
      }}>
      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={handleSave} disabled={!isDirty} style={{
          flex: 1, padding: '8px 0', borderRadius: 8, border: 'none',
          background: isDirty ? 'var(--accent)' : 'var(--bg-input)', color: isDirty ? 'var(--accent-text)' : 'var(--text-muted)',
          fontSize: 12, fontWeight: 600, cursor: isDirty ? 'pointer' : 'default', opacity: isDirty ? 1 : 0.5,
        }}>{i18nT('apps.mochi.settingsPanel.save')}</button>
        <button onClick={handleClose} style={{
          flex: 1, padding: '8px 0', borderRadius: 8,
          border: '1px solid var(--border)', background: 'transparent',
          color: 'var(--text)', fontSize: 12, cursor: 'pointer',
        }}>{i18nT('apps.mochi.settingsPanel.cancel')}</button>
      </div>

      </div>{/* footer */}

      {showUnsaved && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 1000,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{
            background: 'var(--bg)', borderRadius: 10, padding: '16px 20px', width: 260,
            border: '1px solid var(--border)', boxShadow: '0 8px 24px rgba(0,0,0,0.3)',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>{i18nT('apps.mochi.settingsPanel.unsaved_title')}</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 16, lineHeight: 1.4 }}>
              {i18nT('apps.mochi.settingsPanel.unsaved_desc')}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => { setShowUnsaved(false); handleDiscard() }} style={{
                background: 'transparent', border: '1px solid var(--border)', borderRadius: 6,
                padding: '5px 12px', color: 'var(--text)', fontSize: 12, cursor: 'pointer',
              }}>{i18nT('apps.mochi.settingsPanel.discard')}</button>
              <button onClick={() => { setShowUnsaved(false); handleSave() }} style={{
                background: 'var(--accent)', border: 'none', borderRadius: 6,
                padding: '5px 12px', color: 'var(--accent-text)', fontSize: 12, fontWeight: 600, cursor: 'pointer',
              }}>{i18nT('apps.mochi.settingsPanel.save')}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const Section: React.FC<{ title: React.ReactNode; children: React.ReactNode }> = ({ title, children }) => (
  <div style={{ marginBottom: 16 }}>
    <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 8 }}>{title}</div>
    {children}
  </div>
)


const Field: React.FC<{ label: string; desc?: string; value: string; onChange: (v: string) => void }> = ({ label, desc, value, onChange }) => (
  <div style={{ marginBottom: 8 }}>
    <div style={{ fontSize: 12, color: 'var(--text)', marginBottom: 1 }}>{label}</div>
    {desc && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 3 }}>{desc}</div>}
    <input value={value} onChange={(e) => onChange(e.target.value)}
      style={{ width: '100%', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, padding: '5px 8px', color: 'var(--text)', fontSize: 12, outline: 'none' }} />
  </div>
)


const SelectField: React.FC<{ label: string; desc?: string; value: string; options: (string | { value: string; label: string })[]; onChange: (v: string) => void }> = ({ label, desc, value, options, onChange }) => (
  <div style={{ marginBottom: 8 }}>
    <div style={{ fontSize: 12, color: 'var(--text)', marginBottom: 1 }}>{label}</div>
    {desc && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 3 }}>{desc}</div>}
    <select value={value} onChange={(e) => onChange(e.target.value)}
      style={{ width: '100%', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, padding: '5px 8px', color: 'var(--text)', fontSize: 12, outline: 'none' }}>
      {options.map((o) => { const v = typeof o === 'string' ? o : o.value; const l = typeof o === 'string' ? o : o.label; return <option key={v} value={v}>{l}</option> })}
    </select>
  </div>
)


/**
 * Platform-aware accelerator capture.
 *
 * Three things upstream got wrong for non-macOS, all of which fail in a way the
 * user cannot diagnose:
 *
 *  1. Alt was emitted as `Option`, which Electron documents as macOS-ONLY. On
 *     Windows the binding never registers, and the settings UI then reports it
 *     as "already taken" — pointing the user at the wrong problem.
 *  2. A stored `CommandOrControl` was always DISPLAYED as ⌘, so a Windows user
 *     saw a key their keyboard does not have.
 *  3. `Meta` (the Windows key) was displayed as ⌘ and mapped back to
 *     CommandOrControl, silently turning a Win-key combo into a Ctrl one.
 */
const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** KeyboardEvent.key -> the glyph shown in the recorder. */
const KEY_MAP: Record<string, string> = {
  Meta: IS_MAC ? '⌘' : 'Win',
  Control: 'Ctrl',
  Alt: 'Alt',
  Shift: 'Shift',
}

// Accelerator token maps + canonical modifier order live in shared/config
// (ELECTRON_MAP / MODIFIER_GLYPHS / MODIFIER_ORDER) — Electron accelerator
// identifiers, not translatable copy.

function canonicalAccelerator(tokens: string[]): string {
  const mods = tokens.filter((k) => MODIFIER_ORDER.includes(k))
  const rest = tokens.filter((k) => !MODIFIER_ORDER.includes(k))
  mods.sort((a, b) => MODIFIER_ORDER.indexOf(a) - MODIFIER_ORDER.indexOf(b))
  return [...mods, ...rest].join('+')
}

/** Electron accelerator -> glyphs, resolving CommandOrControl for THIS platform. */
function displayAccelerator(value: string): string[] {
  return value
    .replace(/CommandOrControl/g, IS_MAC ? '⌘' : 'Ctrl')
    .replace(/Command/g, '⌘')
    .replace(/Super|Meta/g, IS_MAC ? '⌘' : 'Win')
    .replace(/Option/g, 'Alt')
    .split('+')
}

const ShortcutField: React.FC<{ label: string; desc?: string; value: string; onChange: (v: string) => void }> = ({ label, desc, value, onChange }) => {
  const [listening, setListening] = React.useState(false)
  const [keys, setKeys] = React.useState<string[]>([])
  const keysRef = React.useRef<Set<string>>(new Set())

  const displayValue = displayAccelerator(value)

  const [error, setError] = React.useState('')

  React.useEffect(() => {
    if (!listening) return
    const onDown = (e: KeyboardEvent) => {
      e.preventDefault(); e.stopPropagation()
      setError('')
      const key = KEY_MAP[e.key] || (e.key.length === 1 ? e.key.toUpperCase() : e.key)
      if (keysRef.current.size >= 3) return // max 3 keys
      keysRef.current.add(key)
      setKeys([...keysRef.current])
    }
    const onUp = () => {
      const captured = [...keysRef.current]
      const mods = captured.filter(k => MODIFIER_GLYPHS.includes(k))
      const rest = captured.filter(k => !MODIFIER_GLYPHS.includes(k))
      if (mods.length >= 1 && rest.length === 1 && captured.length >= 2 && captured.length <= 3) {
        onChange(canonicalAccelerator([...mods, ...rest].map(k => ELECTRON_MAP[k] || k)))
        setListening(false); setKeys([]); keysRef.current.clear(); setError('')
      } else if (captured.length > 0) {
        setError(rest.length === 0
      ? i18nT('apps.mochi.settingsPanel.shortcut_need_key')
      : rest.length > 1
        ? i18nT('apps.mochi.settingsPanel.shortcut_one_key')
        : i18nT('apps.mochi.settingsPanel.shortcut_invalid'))
        setKeys([]); keysRef.current.clear()
      }
    }
    window.addEventListener('keydown', onDown, true)
    window.addEventListener('keyup', onUp, true)
    return () => { window.removeEventListener('keydown', onDown, true); window.removeEventListener('keyup', onUp, true) }
  }, [listening, onChange])

  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 12, color: 'var(--text)', marginBottom: 1 }}>{label}</div>
      {desc && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 3 }}>{desc}</div>}
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 4, flex: 1 }}>
          {(listening && keys.length > 0 ? keys : displayValue).map((k, i) => (
            <span key={i} style={{
              padding: '3px 8px', borderRadius: 4, fontSize: 11, fontWeight: 600,
              background: listening ? 'var(--accent-glow)' : 'var(--bg-input)',
              border: `1px solid ${listening ? 'var(--accent)' : 'var(--border)'}`,
              color: 'var(--text)', minWidth: 28, textAlign: 'center',
            }}>{k}</span>
          ))}
          {listening && keys.length === 0 && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic' }}>{i18nT('apps.mochi.settingsPanel.press_keys')}</span>
          )}
        </div>
        <button onClick={() => { setListening(!listening); setKeys([]); keysRef.current.clear(); setError('') }}
          aria-label={listening ? i18nT('apps.mochi.editor.cancel') : i18nT('apps.mochi.settingsPanel.record_shortcut')} style={{
          padding: '3px 10px', borderRadius: 6, fontSize: 11, cursor: 'pointer',
          background: listening ? 'var(--danger)' : 'var(--bg-input)',
          border: `1px solid ${listening ? 'var(--danger)' : 'var(--border)'}`,
          color: listening ? '#fff' : 'var(--text)',
          display: 'inline-flex', alignItems: 'center',
        }}>{listening ? <X size={13} /> : <Keyboard size={13} />}</button>
      </div>
      {error && <div style={{ fontSize: 10, color: 'var(--danger)', marginTop: 2 }}>{error}</div>}
    </div>
  )
}
const Toggle: React.FC<{ label: string; desc?: string; value: boolean; onChange: (v: boolean) => void }> = ({ label, desc, value, onChange }) => (
  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 8, gap: 12 }}>
    <div>
      <span style={{ fontSize: 12 }}>{label}</span>
      {desc && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 1 }}>{desc}</div>}
    </div>
    <div role="switch" aria-checked={value} tabIndex={0}
      onClick={() => onChange(!value)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onChange(!value) } }}
      style={{
      width: 36, height: 20, borderRadius: 10, cursor: 'pointer', flexShrink: 0,
      background: value ? 'var(--accent)' : 'var(--bg-input)',
      border: value ? 'none' : '1px solid var(--border)',
      position: 'relative', transition: 'background 200ms',
    }}>
      <div style={{
        width: 16, height: 16, borderRadius: '50%', background: 'white',
        position: 'absolute', top: 2, left: value ? 18 : 2, transition: 'left 200ms',
      }} />
    </div>
  </div>
)

/* ── EditableField (edit-confirm pattern) ──────────────────── */

/* ── BackendModeSection ────────────────────────────────────────── */
const MOOD_COLORS: Record<string, string> = {
  happy: '#f0abfc',
  curious: '#93c5fd',
  sleepy: '#c4b5fd',
  busy: '#fbbf24',
  scared: '#fca5a5',
  neutral: '#a1a1aa',
}

/**
 * Map a discover-tools failure CODE to a translated message.
 *
 * The backend's `code` is the contract; its English `error` prose is for logs.
 * Anything unrecognised (including a synthetic `http_405` / `network`) falls back
 * to one generic translated line, so no locale ever sees a raw code or HTTP status.
 */
const MCP_ERROR_KEYS = {
  server_disabled: 'apps.mochi.settingsPanel.mcp_error_disabled',
  probe_in_progress: 'apps.mochi.settingsPanel.mcp_error_in_progress',
  server_not_found: 'apps.mochi.settingsPanel.mcp_error_not_found',
  probe_failed: 'apps.mochi.settingsPanel.mcp_error_probe_failed',
} as const

const MCP_ERROR_KEY_FALLBACK = 'apps.mochi.settingsPanel.mcp_error_generic'

function mcpErrorText(code: string): string {
  const key =
    (MCP_ERROR_KEYS as Record<string, string>)[code] ?? MCP_ERROR_KEY_FALLBACK
  return i18nT(key)
}

function moodColor(mood: string): string {
  return MOOD_COLORS[mood] ?? 'var(--accent)'
}

const MemoriesSection: React.FC<{
  stats: CompanionStats | null
  petName: string
}> = ({ stats, petName }) => {
  if (!stats) return null

  const totalMessages = stats.messages.sent + stats.messages.received
  const topMoods = getTopMoods(stats.moods, Infinity)

  const rows: Array<{ Icon: LucideIcon; text: string }> = []

  // Companion time + streak
  if (shouldShowStat(stats.companionSeconds)) {
    let text = i18nT('apps.mochi.stats.companion_days', { time: formatCompanionTime(stats.companionSeconds) })
    if (shouldShowStat(stats.streak)) {
      text += ' ' + i18nT('apps.mochi.stats.streak', { streak: fmtNumber(stats.streak) })
    }
    rows.push({ Icon: Clock, text })
  }

  // Messages
  if (shouldShowStat(totalMessages)) {
    rows.push({
      Icon: MessageCircle,
      text: i18nT('apps.mochi.stats.messages', {
        total: fmtNumber(totalMessages),
        sent: fmtNumber(stats.messages.sent),
        received: fmtNumber(stats.messages.received),
        name: petName,
      }),
    })
  }

  // Walk steps
  if (shouldShowStat(stats.walkSteps)) {
    rows.push({ Icon: PawPrint, text: i18nT('apps.mochi.stats.walks', { count: fmtNumber(stats.walkSteps) }) })
  }

  // Screenshots
  if (shouldShowStat(stats.screenshots)) {
    rows.push({ Icon: Camera, text: i18nT('apps.mochi.stats.screenshots', { count: fmtNumber(stats.screenshots) }) })
  }

  // Peeks
  if (shouldShowStat(stats.peeks)) {
    rows.push({ Icon: EyeOff, text: i18nT('apps.mochi.stats.peeks', { count: fmtNumber(stats.peeks) }) })
  }

  // Drags
  if (shouldShowStat(stats.drags)) {
    rows.push({ Icon: MousePointer, text: i18nT('apps.mochi.stats.drags', { count: fmtNumber(stats.drags) }) })
  }

  // Thinking time
  if (shouldShowStat(stats.thinkingSeconds)) {
    rows.push({ Icon: Brain, text: formatThinkingTime(stats.thinkingSeconds) })
  }

  // Latest active time
  if (shouldShowStat(stats.latestActiveTime)) {
    rows.push({ Icon: Moon, text: i18nT('apps.mochi.stats.latest_time', { time: stats.latestActiveTime }) })
  }

  // Earliest active time
  if (shouldShowStat(stats.earliestActiveTime)) {
    rows.push({ Icon: Sun, text: i18nT('apps.mochi.stats.earliest_time', { time: stats.earliestActiveTime }) })
  }

  // Busiest day — with localized date
  if (shouldShowStat(stats.busiestDay.messages)) {
    rows.push({
      Icon: Flame,
      text: i18nT('apps.mochi.stats.busiest_day', {
        date: formatDate(stats.busiestDay.date),
        count: fmtNumber(stats.busiestDay.messages),
      }),
    })
  }

  // Longest chat
  if (shouldShowStat(stats.longestChat)) {
    rows.push({ Icon: MessageCircle, text: i18nT('apps.mochi.stats.longest_chat', { count: fmtNumber(stats.longestChat) }) })
  }

  // First-use: show a warm welcome if almost no data yet
  const isFirstUse = rows.length <= 1 && topMoods.length === 0

  // If truly nothing at all (not even day 1), hide section
  if (rows.length === 0 && topMoods.length === 0) return null

  return (
    <Section title={<span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}><BookOpen size={13} /> {i18nT('apps.mochi.stats.title')}</span>}>
      <div style={{
        background: 'var(--bg-input)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        padding: '10px 12px',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}>
        {rows.map((row, i) => (
          <div key={i} style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '3px 4px',
            borderRadius: 6,
            transition: 'background 150ms',
          }}>
            <span style={{ flexShrink: 0, width: 20, display: 'flex', justifyContent: 'center', color: 'var(--text-muted)' }}><row.Icon size={13} /></span>
            <span style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.4 }}>{row.text}</span>
          </div>
        ))}

        {/* Mood TOP 3 — with translated mood names */}
        {topMoods.length > 0 && (
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '3px 4px',
            borderRadius: 6,
          }}>
            <span style={{ flexShrink: 0, width: 20, display: 'flex', justifyContent: 'center', color: 'var(--text-muted)' }}><Smile size={13} /></span>
            <span style={{ fontSize: 12, color: 'var(--text)', marginRight: 4, flexShrink: 0, whiteSpace: 'nowrap' }}>{i18nT('apps.mochi.stats.mood_top')}:</span>
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
              {topMoods.map((m) => {
                const label = moodLabel(m.mood)
                return (
                  <span key={m.mood} style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 3,
                    padding: '1px 8px',
                    borderRadius: 10,
                    fontSize: 11,
                    fontWeight: 500,
                    background: `${moodColor(m.mood)}22`,
                    border: `1px solid ${moodColor(m.mood)}44`,
                    color: 'var(--text)',
                  }}>
                    {label} {fmtPercent(m.percent / 100)}
                  </span>
                )
              })}
            </div>
          </div>
        )}

        {/* First-use welcome message */}
        {isFirstUse && (
          <div style={{
            padding: '4px 8px',
            fontSize: 11,
            color: 'var(--text-muted)',
            fontStyle: 'italic',
            textAlign: 'center',
          }}>
            {i18nT('apps.mochi.stats.first_use', { name: petName })}
          </div>
        )}
      </div>
    </Section>
  )
}

/* ── McpSection — MCP server management with tool visibility ───── */

/** Mini toggle matching the existing Toggle component style */
const MiniToggle: React.FC<{ on: boolean; onChange: (v: boolean) => void; label: string }> = ({ on, onChange, label }) => (
  <div role="switch" aria-checked={on} tabIndex={0}
    onClick={() => onChange(!on)}
    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onChange(!on) } }}
    style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
    <div style={{
      width: 28, height: 16, borderRadius: 8, position: 'relative', transition: 'background 200ms',
      background: on ? 'var(--accent)' : 'var(--bg)',
      border: on ? 'none' : '1px solid var(--border)',
    }}>
      <div style={{
        width: 12, height: 12, borderRadius: '50%', background: 'white',
        position: 'absolute', top: 2, left: on ? 14 : 2, transition: 'left 200ms',
      }} />
    </div>
    <span style={{ fontSize: 11, color: 'var(--text)' }}>{label}</span>
  </div>
)

const McpSection: React.FC<{
  config: AppConfig
  editMochi: (partial: Partial<AppConfig['mochi']>) => void
  trustMode: 'normal' | 'trust_reads' | 'trust' | 'yolo'
}> = ({ config, editMochi, trustMode }) => {
  const [servers, setServers] = React.useState<McpServerInfo[]>([])
  const [loading, setLoading] = React.useState(true)
  const [expanded, setExpanded] = React.useState<string | null>(null)
  const [activeTab, setActiveTab] = React.useState<Record<string, 'chat' | 'bg'>>({})
  const [toolsMap, setToolsMap] = React.useState<Record<string, { tools: Array<{ name: string; description?: string }>; fromCache: boolean; errorCode?: string }>>({})
  const [refreshing, setRefreshing] = React.useState<string | null>(null)
  const [stagedConfigs, setStagedConfigs] = React.useState<Record<string, { agents: ('chat' | 'bg')[]; autoApprove: string[]; disabledTools: string[] }>>({})

  React.useEffect(() => {
    api?.getMcpServers?.().then((list: McpServerInfo[]) => {
      if (Array.isArray(list)) setServers(list)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  // Never vanish. Upstream hid the whole section when the list was empty, which
  // in this build reads as "Mochi has no MCP settings" -- the list comes from
  // core's /api/mcp/servers, and an empty or failed read is exactly when the user
  // most needs to see that the section exists.
  if (loading || servers.length === 0) {
    return (
      <Section title={i18nT('apps.mochi.settingsPanel.mcp_title')}>
        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {loading
            ? i18nT('apps.mochi.settingsPanel.mcp_loading')
            : i18nT('apps.mochi.settingsPanel.mcp_empty')}
        </div>
      </Section>
    )
  }

  const extras = config.mochi.extraMcpServers || []
  const extrasNames = new Set(extras.map((e: any) => typeof e === 'string' ? e : e.name))
  const enabled = servers.filter(s => s.core || extrasNames.has(s.name))
  const available = servers.filter(s => !s.core && !extrasNames.has(s.name))

  const toggleServer = (name: string, enable: boolean) => {
    const current = extras.filter((e: any) => (typeof e === 'string' ? e : e.name) !== name)
    if (enable) {
      const staged = stagedConfigs[name]
      current.push(staged ? { name, ...staged } : { name, agents: ['chat'], autoApprove: [], disabledTools: [] })
    }
    editMochi({ extraMcpServers: current })
  }

  const getStaged = (name: string, server: McpServerInfo) =>
    stagedConfigs[name] || { agents: server.agents || ['chat'], autoApprove: server.autoApprove || [], disabledTools: server.disabledTools || [] }

  const updateStaged = (name: string, update: Partial<{ agents: ('chat' | 'bg')[]; autoApprove: string[]; disabledTools: string[] }>) => {
    const server = servers.find(s => s.name === name)
    const current = getStaged(name, server || { agents: ['chat'], autoApprove: [], disabledTools: [] } as unknown as McpServerInfo)
    const newStaged = { ...current, ...update }
    setStagedConfigs(prev => ({ ...prev, [name]: newStaged }))
    const updated = extras.map((e: any) => {
      const eName = typeof e === 'string' ? e : e.name
      return eName === name ? { name, ...newStaged } : e
    })
    editMochi({ extraMcpServers: updated })
  }

  const refreshTools = async (serverName: string) => {
    setRefreshing(serverName)
    try {
      const result = await api?.discoverMcpTools?.(serverName)
      if (result) {
        setToolsMap(prev => {
          const previous = prev[serverName]
          // On failure MERGE onto the previous entry: a transient blip used to
          // return null, which `if (result)` skipped, so the last good tool list
          // stayed on screen. Replacing it with the empty list would make a
          // network hiccup wipe the chip groups a user is mid-way through
          // configuring -- the error message must ADD to the view, not clear it.
          if (result.errorCode && previous) {
            return { ...prev, [serverName]: { ...previous, errorCode: result.errorCode } }
          }
          return { ...prev, [serverName]: result }
        })
      }
    } catch { /* ignore */ }
    setRefreshing(null)
  }

  const agentLabel = (agents: ('chat' | 'bg')[]) => {
    const c = agents.includes('chat'), b = agents.includes('bg')
    if (c && b) return i18nT('apps.mochi.settingsPanel.mcp_agent_chat_bg')
    if (b) return i18nT('apps.mochi.settingsPanel.mcp_agent_bg_only')
    return i18nT('apps.mochi.settingsPanel.mcp_agent_chat_only')
  }

  const toggleAgent = (name: string, agent: 'chat' | 'bg', on: boolean, server: McpServerInfo) => {
    const staged = getStaged(name, server)
    const agentSet = new Set(staged.agents)
    if (on) agentSet.add(agent); else agentSet.delete(agent)
    updateStaged(name, { agents: [...agentSet] })
  }

  const renderExpandedServer = (s: McpServerInfo) => {
    const staged = getStaged(s.name, s)
    const tab = activeTab[s.name] || 'chat'
    const toolData = toolsMap[s.name]
    const allToolNames = toolData?.tools.map(t => t.name) || []

    // Use effective policies from the server info (computed by main process with presets)
    const policy = tab === 'chat' ? s.chatPolicy : s.bgPolicy
    const effectiveApprove = new Set(policy?.autoApprove || [])
    const effectiveDisabled = new Set(policy?.disabledTools || [])

    const autoApproved = allToolNames.filter((t: string) => effectiveApprove.has(t))
    const disabled = allToolNames.filter((t: string) => effectiveDisabled.has(t))
    const needsApproval = allToolNames.filter((t: string) => !effectiveApprove.has(t) && !effectiveDisabled.has(t))

    // Stale tools
    const allToolSet = new Set(allToolNames)
    const staleTools = [...(policy?.autoApprove || []), ...(policy?.disabledTools || [])].filter((t: string) => allToolNames.length > 0 && !allToolSet.has(t))

    return (
      <div style={{ padding: '6px 0 2px 0' }}>
        {/* Agent toggles */}
        {!s.core && (
          <div style={{ display: 'flex', gap: 16, marginBottom: 8 }}>
            <MiniToggle on={staged.agents.includes('chat')} onChange={(v) => toggleAgent(s.name, 'chat', v, s)}
              label={i18nT('apps.mochi.settingsPanel.mcp_agent_chat')} />
            <MiniToggle on={staged.agents.includes('bg')} onChange={(v) => toggleAgent(s.name, 'bg', v, s)}
              label={i18nT('apps.mochi.settingsPanel.mcp_agent_bg')} />
          </div>
        )}
        {/* Chat / BG tab selector */}
        <div style={{ display: 'flex', gap: 0, marginBottom: 6 }}>
          {(['chat', 'bg'] as const).map(t => (
            <button key={t} onClick={() => setActiveTab(prev => ({ ...prev, [s.name]: t }))} style={{
              fontSize: 10, padding: '3px 10px', cursor: 'pointer', border: '1px solid var(--border)',
              background: tab === t ? 'var(--accent-glow)' : 'var(--bg)',
              color: tab === t ? 'var(--accent)' : 'var(--text-muted)',
              fontWeight: tab === t ? 600 : 400,
              borderRadius: t === 'chat' ? '4px 0 0 4px' : '0 4px 4px 0',
              borderLeft: t === 'bg' ? 'none' : undefined,
            }}>{t === 'chat' ? i18nT('apps.mochi.settingsPanel.mcp_tab_chat') : i18nT('apps.mochi.settingsPanel.mcp_tab_bg')}</button>
          ))}
        </div>
        {/* Refresh tools */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
          <button onClick={() => refreshTools(s.name)} disabled={refreshing === s.name} style={{
            fontSize: 10, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
            background: 'var(--bg)', border: '1px solid var(--border)', color: 'var(--text)',
            opacity: refreshing === s.name ? 0.5 : 1,
          }}>{refreshing === s.name ? i18nT('apps.mochi.settingsPanel.mcp_refreshing') : i18nT('apps.mochi.settingsPanel.mcp_refresh_tools')}</button>
          {toolData?.fromCache && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{i18nT('apps.mochi.settingsPanel.mcp_from_cache')}</span>}
          {/* A failed discover used to be indistinguishable from a server with no
              tools, which is how a 405 on this button went unnoticed. The live
              region is mounted UNCONDITIONALLY and its text swapped: a
              role="status" node that appears together with its content is not
              announced by many screen-reader/browser pairs, which would leave AT
              users with exactly the silence this fix set out to break. */}
          <span
            role="status"
            aria-live="polite"
            style={{ fontSize: 11, color: 'var(--danger, #e5484d)' }}
          >
            {toolData?.errorCode ? mcpErrorText(toolData.errorCode) : ''}
          </span>
        </div>
        {/* Tool lists */}
        {!toolData && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>{i18nT('apps.mochi.settingsPanel.mcp_no_tools')}</div>}
        {toolData && (
          <div style={{ marginTop: 2, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {renderToolGroup(i18nT('apps.mochi.settingsPanel.mcp_auto_approved'), autoApproved, '#22c55e')}
            {renderToolGroup(i18nT('apps.mochi.settingsPanel.mcp_disabled'), disabled, 'var(--danger, #ef4444)')}
            {renderToolGroup(i18nT('apps.mochi.settingsPanel.mcp_needs_approval'), needsApproval, 'var(--text-muted)')}
            {staleTools.length > 0 && renderToolGroup(i18nT('apps.mochi.settingsPanel.mcp_stale'), staleTools, 'var(--text-muted)', true)}
          </div>
        )}
      </div>
    )
  }

  const renderToolGroup = (label: string, tools: string[], color: string, stale = false) => {
    if (tools.length === 0) return null
    return (
      <div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>
          {label} <span style={{ opacity: 0.6 }}>({tools.length})</span>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
          {tools.map((t: string) => (
            <span key={t} style={{
              fontSize: 10, padding: '1px 6px', borderRadius: 3,
              background: stale ? 'var(--bg)' : 'var(--bg-input)',
              border: `1px solid ${stale ? 'var(--border)' : color}`,
              color: stale ? 'var(--text-muted)' : color,
              opacity: stale ? 0.5 : 1,
              textDecoration: stale ? 'line-through' : 'none',
            }}>{t}</span>
          ))}
        </div>
      </div>
    )
  }

  return (
    <Section title={i18nT('apps.mochi.settingsPanel.mcp_title')}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6 }}>
        {i18nT('apps.mochi.settingsPanel.mcp_desc', { name: config.mochi.petName || 'Mochi' })}
      </div>
      <div style={{
        fontSize: 10, marginBottom: 6, padding: '4px 8px', borderRadius: 6,
        background: (trustMode === 'normal' || trustMode === 'trust_reads') ? 'var(--accent-glow)' : 'rgba(234,179,8,0.1)',
        color: (trustMode === 'normal' || trustMode === 'trust_reads') ? 'var(--accent)' : '#ca8a04',
        border: `1px solid ${(trustMode === 'normal' || trustMode === 'trust_reads') ? 'var(--accent)' : 'rgba(234,179,8,0.3)'}`,
      }}>
        {/* The ✓ / ⚠ that used to lead these strings is an icon, so it is drawn
            as one — it then follows the banner's own colour and size instead of
            the font's, and does not need repeating in ten locales. */}
        {(trustMode === 'normal' || trustMode === 'trust_reads')
          ? <Check size={11} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 4 }} />
          : <AlertTriangle size={11} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 4 }} />}
        {trustMode === 'yolo' ? i18nT('apps.mochi.settingsPanel.mcp_trust_warn_auto')
          : trustMode === 'trust' ? i18nT('apps.mochi.settingsPanel.mcp_trust_warn_trust')
          : trustMode === 'trust_reads' ? i18nT('apps.mochi.settingsPanel.mcp_trust_reads_hint')
          : i18nT('apps.mochi.settingsPanel.mcp_trust_ok')}
      </div>
      <div style={{
        background: 'var(--bg-input)', border: '1px solid var(--border)',
        borderRadius: 10, padding: '8px 10px',
        display: 'flex', flexDirection: 'column', gap: 4,
      }}>
        {enabled.map((s) => (
          <div key={s.name}>
            <div role="button" tabIndex={0} aria-expanded={expanded === s.name} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '4px 6px', borderRadius: 6, cursor: 'pointer',
            }} onClick={() => {
              const opening = expanded !== s.name
              setExpanded(opening ? s.name : null)
              if (opening && !toolsMap[s.name]) refreshTools(s.name)
            }} onKeyDown={(e) => {
              if (e.key !== 'Enter' && e.key !== ' ') return
              e.preventDefault()
              const opening = expanded !== s.name
              setExpanded(opening ? s.name : null)
              if (opening && !toolsMap[s.name]) refreshTools(s.name)
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#22c55e', flexShrink: 0 }} />
                <span style={{ fontSize: 12, color: 'var(--text)' }}>{s.name}</span>
                {s.core && <span style={{ fontSize: 10, color: 'var(--text-muted)', fontStyle: 'italic' }}>{i18nT('apps.mochi.settingsPanel.core')}</span>}
                <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>{agentLabel(getStaged(s.name, s).agents)}</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                {!s.core && (
                  <button onClick={(e) => { e.stopPropagation(); toggleServer(s.name, false) }}
                    aria-label={i18nT('apps.mochi.settingsPanel.remove_server', { name: s.name })} style={{
                    background: 'none', border: 'none', color: 'var(--text-muted)',
                    cursor: 'pointer', fontSize: 11, padding: '2px 6px', borderRadius: 4,
                    display: 'inline-flex', alignItems: 'center',
                  }}><X size={12} /></button>
                )}
                <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{expanded === s.name ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
              </div>
            </div>
            {expanded === s.name && renderExpandedServer(s)}
          </div>
        ))}
        {available.length > 0 && (
          <>
            <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
            <div style={{ fontSize: 10, color: 'var(--text-muted)', padding: '2px 6px' }}>
              {i18nT('apps.mochi.settingsPanel.mcp_available')}:
            </div>
            {available.map((s) => (
              <div key={s.name} style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '4px 6px', borderRadius: 6,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <div style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--border)', flexShrink: 0 }} />
                  <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{s.name}</span>
                </div>
                <button onClick={() => toggleServer(s.name, true)} style={{
                  background: 'var(--accent-glow)', border: '1px solid var(--accent)',
                  color: 'var(--accent)', cursor: 'pointer', fontSize: 10, padding: '2px 8px',
                  borderRadius: 4, fontWeight: 600,
                }}><Plus size={10} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 2 }} />{i18nT('apps.mochi.settingsPanel.mcp_add')}</button>
              </div>
            ))}
          </>
        )}
      </div>
    </Section>
  )
}

/* ── ModelSelector ────────────────────────────────────────────── */

/* ── BackgroundActivitySection ─────────────────────────────────────
   The SPEND axis, deliberately separate from Behavior (the personality
   axis): neither reads the other's key, so making the pet chattier can
   never silently cost more. Tier cards state the concrete contract
   (runs/hour, floor, batch) rather than adjectives, and the usage line
   reads the same ledger the cap enforces. */
const BackgroundActivitySection: React.FC<{
  config: AppConfig
  editMochi: (patch: Record<string, unknown>) => void
}> = ({ config, editMochi }) => {
  const [usage, setUsage] = React.useState<{ runsThisHour: number; runsToday: number; runs7d: number } | null>(null)

  React.useEffect(() => {
    // Plain fetch, not React Query: this section renders in Mochi's own Electron
    // windows (settings/main.tsx, ChatApp), which mount with a bare `createRoot`
    // and no QueryClientProvider — `useQuery` here throws "No QueryClient set".
    //
    // Refetched on focus because the numbers move while the window is NOT being
    // looked at: the user opens this section to decide a tier, leaves Mochi to
    // run in the background, and comes back. Mount-only would then show the
    // counts from before those runs while the cap enforced the real ones.
    let alive = true
    const load = () => {
      fetch('/api/apps/mochi/bg-usage', { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (alive && d?.usage) setUsage(d.usage) })
        .catch(() => {})
    }
    load()
    window.addEventListener('focus', load)
    return () => { alive = false; window.removeEventListener('focus', load) }
  }, [])

  const tier = (config.mochi.activityTier as string) ?? 'balanced'
  const tiers = [
    { value: 'economy', label: i18nT('apps.mochi.settingsPanel.tier_economy'), desc: i18nT('apps.mochi.settingsPanel.tier_economy_desc') },
    { value: 'balanced', label: i18nT('apps.mochi.settingsPanel.tier_balanced'), desc: i18nT('apps.mochi.settingsPanel.tier_balanced_desc') },
    { value: 'active', label: i18nT('apps.mochi.settingsPanel.tier_active'), desc: i18nT('apps.mochi.settingsPanel.tier_active_desc') },
    { value: 'unlimited', label: i18nT('apps.mochi.settingsPanel.tier_unlimited'), desc: i18nT('apps.mochi.settingsPanel.tier_unlimited_desc') },
  ]

  return (
    <Section title={i18nT('apps.mochi.settingsPanel.bg_activity')}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6 }}>
        {i18nT('apps.mochi.settingsPanel.bg_activity_desc', { name: (config.mochi.petName as string) || 'Mochi' })}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 8 }}>
        {tiers.map((opt) => (
          <label key={opt.value} onClick={() => editMochi({ activityTier: opt.value })} style={{
            display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer',
            padding: '6px 8px', borderRadius: 6,
            background: tier === opt.value ? 'var(--accent-glow)' : 'transparent',
            border: tier === opt.value ? '1px solid var(--accent)' : '1px solid transparent',
          }}>
            <div style={{
              width: 14, height: 14, borderRadius: '50%', flexShrink: 0, marginTop: 1,
              border: tier === opt.value ? '4px solid var(--accent)' : '2px solid var(--border)',
              background: 'var(--bg)',
            }} />
            <div>
              <div style={{ fontSize: 12, color: 'var(--text)', fontWeight: tier === opt.value ? 600 : 400 }}>{opt.label}</div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{opt.desc}</div>
            </div>
          </label>
        ))}
      </div>
      {usage && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {i18nT('apps.mochi.settingsPanel.bg_usage', {
            hour: String(usage.runsThisHour), day: String(usage.runsToday), week: String(usage.runs7d),
          })}
        </div>
      )}
    </Section>
  )
}


const ModelSelector: React.FC<{
  config: AppConfig
  editMochi: (patch: Record<string, unknown>) => void
}> = ({ config, editMochi }) => {
  const [models, setModels] = React.useState<Array<{ model_name: string; model_id?: string; description?: string }>>([])
  const [current, setCurrent] = React.useState('')
  const [loading, setLoading] = React.useState(true)
  const [switching, setSwitching] = React.useState(false)

  React.useEffect(() => {
    api?.getModels?.().then((list: any) => {
      if (Array.isArray(list) && list.length > 0) {
        // Normalize: API may return objects or strings
        const normalized = list.map((m: any) =>
          typeof m === 'string' ? { model_name: m } : m
        )
        setModels(normalized)
      }
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const handleChange = async (model: string) => {
    setCurrent(model)
    setSwitching(true)
    try { await api?.setModel?.(model) } catch {}
    setSwitching(false)
  }

  if (loading || models.length === 0) return null

  // Chat and background model selection live in ONE section so the two rows
  // share the same field styling instead of one section header and one bare
  // label drifting apart.
  const chatOptions = [
    { value: '', label: i18nT('apps.mochi.settingsPanel.model_auto') },
    ...models.map((m) => {
      const id = m.model_id || m.model_name
      return { value: id, label: m.model_name }
    }),
  ]
  const bgOptions = [
    { value: '', label: i18nT('apps.mochi.settingsPanel.bg_model_default') },
    ...models.map((m) => {
      const id = m.model_id || m.model_name
      return { value: id, label: m.model_name }
    }),
  ]

  return (
    <Section title={i18nT('apps.mochi.settingsPanel.model')}>
      <SelectField label={i18nT('apps.mochi.settingsPanel.chat_model')} desc={i18nT('apps.mochi.settingsPanel.model_desc')}
        value={current}
        options={chatOptions}
        onChange={(v) => void handleChange(v)} />
      {switching && (
        <div style={{ fontSize: 10, color: 'var(--accent)', marginTop: -4, marginBottom: 6 }}>
          {i18nT('apps.mochi.settingsPanel.model_switching')}
        </div>
      )}
      <SelectField label={i18nT('apps.mochi.settingsPanel.bg_model')} desc={i18nT('apps.mochi.settingsPanel.bg_model_desc')}
        value={(config.mochi.bgModel as string) ?? ''}
        options={bgOptions}
        onChange={(v) => editMochi({ bgModel: v })} />
    </Section>
  )
}
