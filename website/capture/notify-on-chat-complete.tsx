/**
 * Isolated capture entry for the "notify when a background chat finishes" toggle
 * (the new Desktop alerts section this PR adds to Settings > Notifications).
 *
 * WHY ISOLATED: the delta is one new SettingsSection carrying one SettingsToggle;
 * the rest of NotificationsPanel (sound presets, per-category rows, and the
 * ChannelsSection that fetches notification sources against a live gateway) is
 * unchanged and would only add noise — a red "couldn't load sources" banner from
 * a fetch this harness does not serve. So this mounts the REAL section markup
 * from NotificationsPanel — the same `SettingsSection` / `SettingsCard` /
 * `SettingsToggle` components and the same i18n keys — and nothing else.
 *
 * One frame, both states side by side: the toggle OFF (its default, the state a
 * first visit shows) and ON (what the user sees after enabling it, the gesture
 * that also arms the OS permission prompt). `?theme=` selects dark or light.
 */
import { createRoot } from 'react-dom/client'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the section renders blank.
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { SettingsSection, SettingsCard, SettingsToggle } from '../src/components/settings'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** The exact section this PR adds to NotificationsPanel, rendered with a fixed
 *  `checked` so each pane is one deterministic state. `onChange` is a no-op: the
 *  frame captures the control's appearance, not its persistence. */
function Section({ checked }: { checked: boolean }) {
  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.desktop_alerts')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.notificationsPanel.notify_when_a_background_chat_finishes')}
          description={i18nT('pages.settings.notificationsPanel.notify_when_a_background_chat_finishes_description')}
          checked={checked}
          onChange={() => {}}
        />
      </SettingsCard>
    </SettingsSection>
  )
}

function Pane({ checked, label, caption }: { checked: boolean; label: string; caption: string }) {
  return (
    <div className="flex flex-col gap-2 w-[460px] shrink-0">
      <div className="text-text text-[13px] font-medium">{label}</div>
      <div className="text-muted text-[11px] leading-snug pr-10">{caption}</div>
      <Section checked={checked} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div className="bg-bg p-6 flex gap-10 items-start min-h-screen">
    <Pane
      checked={false}
      label="Default — off"
      caption="A background turn finishing raises no OS notification; the sound chime below is unaffected."
    />
    <Pane
      checked
      label="Enabled"
      caption="Enabling it is the user gesture the OS permission prompt needs; a finishing background chat then raises a notification titled with that session, only while this window is away."
    />
  </div>,
)
