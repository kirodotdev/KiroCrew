import { useId } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { SettingsToggle } from '../../components/settings'
import { useAppSelector } from '../../store'
import { gatewayAutoUpdateCopy, useGatewayAutoUpdateEffect } from '../../utils/updateSwitches'
import { autoUpdatePinNote, useGatewayAutoUpdate, type SwitchState } from '../../hooks/useGatewayAutoUpdate'
import { useAppAutoDownload } from '../../hooks/useAppAutoDownload'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/**
 * A switch row's notices: the saved value could not be read (the switch then
 * shows its default), a read failed while the switch shows a value known
 * otherwise (the last value read, or a save's answer), or the write failed or
 * was refused. askAgent on: the switch shows the value still
 * in effect, so nothing is lost by leaving.
 */
function SwitchNotices({ state, unreadable, testIdPrefix, onHandoff }: {
  state: SwitchState
  /** Omitted where another notice on the page already reports the read. */
  unreadable?: string
  testIdPrefix: string
  onHandoff?: () => void
}) {
  return (
    <>
      {unreadable && state.readFailed && (
        <ErrorNotice variant="inline" message={unreadable} askAgent onHandoff={onHandoff}
          testId={`${testIdPrefix}-read-error`} />
      )}
      {state.refreshFailed && (
        <ErrorNotice variant="inline" message={state.refreshFailed}
          askAgent onHandoff={onHandoff} testId={`${testIdPrefix}-refresh-error`} />
      )}
      {state.saveError && (
        <ErrorNotice variant="inline" message={state.saveError} askAgent onHandoff={onHandoff}
          testId={`${testIdPrefix}-save-error`} />
      )}
    </>
  )
}

/**
 * The desktop app updater's auto-download opt-out, which is ON by default.
 * Shared with the What's-new modal; where each surface draws it is
 * `utils/updateSwitches`. Held at the shell's default, on, until the
 * updater's getInfo() answers.
 */
export function AppAutoDownloadSwitch({ unreadable, anchor = true, onHandoff }: {
  /** The read-failure copy; omitted where the page already reports it. */
  unreadable?: string
  /** `false` for a copy outside Settings, so deep links land on About's row. */
  anchor?: boolean
  onHandoff?: () => void
}) {
  const app = useAppAutoDownload()
  return (
    <div className="pt-1 border-t border-border">
      <SettingsToggle
        label={i18nT('pages.settings.aboutPanel.install_app_updates_automatically')}
        description={i18nT('pages.settings.aboutPanel.install_app_updates_automatically_hint')}
        checked={app.checked}
        disabled={app.held}
        onChange={app.set}
        anchor={anchor}
      />
      <SwitchNotices state={app} testIdPrefix="auto-download" onHandoff={onHandoff} unreadable={unreadable} />
    </div>
  )
}

/**
 * The gateway's `auto_update` switch, shared with the What's-new modal. One
 * row in every state: what changes with the gateway's `update_auto_effect` is
 * whether the switch can be flipped and the note under it
 * (`gatewayAutoUpdateCopy`), so a verdict that arrives while the row is on
 * screen never swaps it for another element. Where the effect holds it, it
 * reads off, since nothing installs there. Held at the schema default, on,
 * until the saved value has been read. Where config.local.json sets the key,
 * the switch is held and its note says why (`autoUpdatePinNote`): the policy
 * that keeps it there when an edition owns the entry, else how to remove it.
 */
export function GatewayAutoUpdateSwitch({ anchor = true, readOnMount = false, onHandoff, contextNote, sectionHeading }: {
  /** `false` for a copy outside Settings, so deep links land on About's row. */
  anchor?: boolean
  /** Re-read the saved value on every mount, not only once it is 30 s old. */
  readOnMount?: boolean
  onHandoff?: () => void
  /** Optional sub-heading rendered inside the gateway section (below the divider),
   *  before the context note and toggle. Makes the gateway controls visually
   *  separate from the app's own update card above. */
  sectionHeading?: string
  /** Optional context note rendered below the section heading, before the toggle. */
  contextNote?: string

}) {
  // `update_can_apply` answers for `POST /api/update` alone; a managed venv whose
  // policy turns unattended installs off still installs through its own Update.
  const canApply = useAppSelector(s =>
    s.dashboard.status?.update_can_apply === true || s.dashboard.status?.update_auto_manual_only === true)
  const copy = gatewayAutoUpdateCopy(useGatewayAutoUpdateEffect(), { canApply })
  const gw = useGatewayAutoUpdate({ readOnMount })
  const reduceMotion = useReducedMotion()
  const noteId = useId()
  const pinNoteId = useId()
  // A refusal already says the same thing, through its notice.
  const showPinNote = gw.overlayPinned && !gw.saveError
  const describedBy = [copy.hint ? noteId : '', showPinNote ? pinNoteId : ''].filter(Boolean).join(' ') || undefined
  return (
    <div className="pt-2.5 border-t border-border">
      {sectionHeading && (
        <p className="text-[13px] font-semibold text-text mb-0.5">{sectionHeading}</p>
      )}
      {contextNote && (
        <p className="text-[13px] text-muted mb-2">{contextNote}</p>
      )}
      <SettingsToggle
        label={i18nT('pages.settings.aboutPanel.automatic_updates')}
        description={i18nT('pages.settings.aboutPanel.gateway_auto_update_description')}
        // A switch held by the effect cannot install whatever is saved, so it
        // reads off; the saved value comes back if the effect turns to install.
        checked={gw.checked && !copy.disabled}
        disabled={copy.disabled || gw.held || gw.overlayPinned}
        onChange={gw.set}
        configKey="auto_update"
        describedBy={describedBy}
        anchor={anchor}
      />
      <AnimatePresence initial={false}>
        {copy.hint && (
          <motion.p
            key="note"
            id={noteId}
            className="text-[12px] text-muted overflow-hidden"
            initial={reduceMotion ? false : { opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={reduceMotion ? undefined : { opacity: 0, height: 0 }}
            data-testid="auto-update-note"
          >
            {copy.hint}
          </motion.p>
        )}
      </AnimatePresence>
      {showPinNote && (
        <p id={pinNoteId} className="text-[12px] text-muted mt-1" data-testid="auto-update-pin-note">
          {autoUpdatePinNote(gw.overlayPolicy)}
        </p>
      )}
      <SwitchNotices state={gw} testIdPrefix="auto-update" onHandoff={onHandoff}
        unreadable={i18nT('pages.settings.aboutPanel.auto_update_setting_unavailable')} />
    </div>
  )
}
