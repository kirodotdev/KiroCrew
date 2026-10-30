import { ShieldCheck } from 'lucide-react'
import { createPortal } from 'react-dom'
import { localOverlayContainer } from './overlayRoot'
import { i18nT } from '../i18n/t'

/**
 * Shown when an enterprise login gate closes on a dashboard already in use.
 * KiroPrerequisiteGate renders it beside the app, never in place of it, so
 * page drafts stay mounted.
 *
 * It is display only: `pointer-events-none`, nothing focusable, no event
 * handlers. Every click, key, focus and scroll goes to whatever is under it,
 * exactly as if it were not there, so it cannot close or block an open
 * dialog or popover, or touch a draft in one. It sits above every modal layer
 * (topmost is z-[10001]) so it stays readable.
 *
 * It describes the LOCAL gateway, so it portals into the local pane's overlay
 * container, which App hides while a remote Crew is in front. The gate renders
 * outside that pane's React tree, so it finds the container by
 * `localOverlayContainer()` and falls back to the body only where there is no
 * local pane (an embedded or test tree).
 *
 * Sign-in happens in a terminal. The status poll keeps probing while the gate
 * is closed, so the notice goes away on its own once the gate reopens.
 *
 * Props: `owner` is false for a viewer who cannot sign the gateway in, who is
 * told to ask the owner instead. `exactCommandElsewhere` is set when the
 * gateway's own login command is not the bare `kiro-cli login` (the desktop
 * app's bundled copy, or a `KIROCREW_KIRO_BIN` override off PATH). That
 * command can be a long quoted path this card cannot let anyone copy, so the
 * card shows no command at all and instead points to the sign-in screen a new
 * window opens on, which shows the exact one with a copy button. Naming the
 * bare command there would hand that owner a command that is not theirs.
 */
export const BARE_LOGIN_COMMAND = 'kiro-cli login'

export default function KiroPrerequisiteRelockNotice({
  owner,
  exactCommandElsewhere,
}: {
  owner: boolean
  exactCommandElsewhere: boolean
}) {
  return createPortal(
    (
      <div
        role="alert"
        data-testid="enterprise-relock-notice"
        className="pointer-events-none fixed left-safe right-safe top-safe-offset-4 z-[10002] mx-auto w-[calc(100%-2rem)] max-w-xl select-none rounded-xl border border-accent/60 bg-card p-4 shadow-2xl"
      >
        <p className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          <ShieldCheck className="lucide-inline" />
          {i18nT('components.kiroPrerequisiteGate.sign_in_required')}
        </p>
        {owner && exactCommandElsewhere ? (
          <p className="mt-1 text-sm font-semibold leading-relaxed text-text-strong">
            {i18nT('components.kiroPrerequisiteGate.relock_exact_command_hint')}
          </p>
        ) : owner ? (
          <>
            <p className="mt-1 text-sm font-semibold text-text-strong">
              {i18nT('components.kiroPrerequisiteGate.relock_run_in_terminal')}
            </p>
            <code className="mt-2 block font-mono text-[13px] text-text">{BARE_LOGIN_COMMAND}</code>
          </>
        ) : (
          <p className="mt-1 text-sm leading-relaxed text-text-strong">
            {i18nT('components.kiroPrerequisiteGate.relock_ask_owner')}
          </p>
        )}
      </div>
    ),
    localOverlayContainer() ?? document.body,
  )
}
