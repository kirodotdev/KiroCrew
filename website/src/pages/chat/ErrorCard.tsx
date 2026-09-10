import { memo } from 'react'
import { KeyRound, Loader2, Play, Settings, SlidersHorizontal } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { chatErrorDisplayText } from '../../lib/chatErrorRecovery'
import type { ChatMessage } from '../../types'

/** Row kind the backend stamps on a terminal model-entitlement rejection
 *  (`chat_utils.MODEL_UNENTITLED_KIND`). Both carriers are load-bearing for the
 *  same reason as `isRetryNotice`: the live broadcast ships `kind`, a rebuilt
 *  transcript `meta.kind`. */
const MODEL_UNENTITLED_KIND = 'model_unentitled'

export const isModelUnentitled = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === MODEL_UNENTITLED_KIND || (m.meta as { kind?: string } | undefined)?.kind === MODEL_UNENTITLED_KIND

/** Row kind the backend stamps on the terminal error an `AcpAuthRequired` turn
 *  produces (`chat_utils.AUTH_REQUIRED_KIND`): the agent process reported it is
 *  not signed in. Same two carriers as above. */
const AUTH_REQUIRED_KIND = 'auth_required'

export const isAuthRequired = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === AUTH_REQUIRED_KIND || (m.meta as { kind?: string } | undefined)?.kind === AUTH_REQUIRED_KIND

export interface ErrorCardProps {
  /** Server- or client-authored prose; typed diagnostic prefixes are display-only. */
  content: string
  meta?: ChatMessage['meta']
  /**
   * True on a `model_unentitled` row rendered by a surface that cannot offer
   * one or both fix actions (a pane has no picker; an embed or popout has no
   * settings route). The prose still names "the model picker" and "Settings →
   * Chat", so the card says where those live instead of leaving the reader
   * with an instruction and nothing to press.
   */
  unentitledElsewhere?: boolean
  /**
   * Continue handler. Passed ONLY for the newest error row of a slot whose last
   * turn ended without a reply — a historical error further up the transcript is
   * settled and must not offer to resume anything.
   */
  onContinue?: () => void
  /** True while a continue request is in flight, so the press cannot double-fire. */
  continuing?: boolean
  /**
   * The fix affordances for a model-entitlement rejection. When set, the card
   * offers them INSTEAD of Continue: the backend has said retrying cannot help,
   * so a resume button on this row would only replay the same rejection.
   * `onPickModel` opens the session's model picker; `onOpenDefaultModel` deep
   * links to Settings → Chat → Default Model, the value every new session
   * inherits and the one that keeps re-creating this error until it changes.
   */
  onPickModel?: () => void
  onOpenDefaultModel?: () => void
  /**
   * The fix affordance for an `auth_required` row: deep link to the Kiro
   * sign-in card in Settings, where the user signs in to Kiro Crew's own
   * identity again. Offered INSTEAD of Continue for the same reason as the
   * entitlement actions -- a retry hits the same signed-out wall -- and on
   * EVERY such row, because a lapsed sign-in is settled state the user still
   * has to act on. Omitted on a surface with no settings route (embed, popout).
   */
  onOpenSignIn?: () => void
}

const ACTION_BTN =
  'shrink-0 inline-flex items-center gap-2 text-[12px] leading-5 font-medium px-3 py-1 rounded-md border-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-colors'
/**
 * The error row in a chat transcript.
 *
 * Every one of these carries prose that already tells the reader to retry
 * ("⟳ Connection lost — please retry."), but until now there was nothing to
 * click: recovering meant retyping the prompt. When the turn is genuinely
 * resumable the card grows an action, so the instruction and the affordance sit
 * in the same place.
 *
 * The button is deliberately absent rather than disabled when the turn is not
 * resumable — a permanently greyed control on a red card reads as a broken
 * feature, and there is no state the user could reach that would enable it.
 *
 * A model-entitlement rejection is the one error whose fix is NOT a retry, so
 * its row swaps Continue for the two actions that actually end it: pick a model
 * the account is served, and change the default the next session would start on.
 */
export const ErrorCard = memo(function ErrorCard({
  content,
  meta,
  onContinue,
  continuing,
  onPickModel,
  onOpenDefaultModel,
  onOpenSignIn,
  unentitledElsewhere,
}: ErrorCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (onOpenSignIn) {
    // A signed-out agent process: the one action that ends it is signing in
    // again from Settings. The prose (the backend's own wording, which may
    // still mention `kiro-cli login` for a kiro-cli-owned process) stays; the
    // button is the in-product path for the Crew-owned one.
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
        data-auth-required="true"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {content}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onOpenSignIn}
            className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
            title={i18nT('pages.chat.errorCard.sign_in_hint')}
            data-testid="error-card-sign-in"
          >
            <KeyRound size={12} className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT('pages.chat.errorCard.sign_in')}
          </button>
        </div>
      </div>
    )
  }
  const displayText = chatErrorDisplayText(content, meta)
  const unentitledActions = onPickModel || onOpenDefaultModel
  // Name only the affordance THIS surface lacks: a pane has neither, an
  // embed/popout has the picker but not the settings route. Saying "the
  // picker is elsewhere" beside a live picker button misleads.
  const elsewhereKey = !unentitledElsewhere
    ? null
    : !onPickModel && !onOpenDefaultModel
      ? 'pages.chat.errorCard.elsewhere_hint'
      : onPickModel && !onOpenDefaultModel
        ? 'pages.chat.errorCard.elsewhere_settings_hint'
        : null
  const elsewhere = elsewhereKey !== null
  if (unentitledActions) {
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {displayText}
        </div>
        {onPickModel && onOpenDefaultModel && (
          // Both actions are needed, and a primary/secondary pair reads as
          // pick-one. Say the dependency on the card itself, not in a tooltip,
          // and quote the two button labels so the pair cannot read as the
          // same action twice.
          <div className="text-[12px] leading-5 text-muted" data-testid="error-card-both-hint">
            {i18nT('pages.chat.errorCard.both_hint', {
              pick: i18nT('pages.chat.errorCard.pick_model'),
              default: i18nT('pages.chat.errorCard.default_model'),
            })}
          </div>
        )}
        {elsewhere && (
          <div className="text-[12px] leading-5 text-muted" data-testid="error-card-elsewhere-hint">
            {i18nT(elsewhereKey!)}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {onPickModel && (
            <button
              type="button"
              onClick={onPickModel}
              className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
              title={i18nT('pages.chat.errorCard.pick_model_hint')}
              data-testid="error-card-pick-model"
            >
              <SlidersHorizontal size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {i18nT('pages.chat.errorCard.pick_model')}
            </button>
          )}
          {onOpenDefaultModel && (
            <button
              type="button"
              onClick={onOpenDefaultModel}
              className={`${ACTION_BTN} bg-transparent text-text ring-1 ring-inset ring-border hover:bg-bg-elevated`}
              title={i18nT('pages.chat.errorCard.default_model_hint')}
              data-testid="error-card-default-model"
            >
              <Settings size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {i18nT('pages.chat.errorCard.default_model')}
            </button>
          )}
        </div>
      </div>
    )
  }
  if (!onContinue) {
    return (
      <div
        className="bg-danger-subtle text-danger text-[13px] leading-5 px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-danger/15 self-center animate-scale-in"
        data-testid="error-card"
        style={{ overflowWrap: 'anywhere' }}
      >
        {displayText}
        {elsewhere && (
          <div className="text-[12px] leading-5 text-muted mt-1" data-testid="error-card-elsewhere-hint">
            {i18nT(elsewhereKey!)}
          </div>
        )}
      </div>
    )
  }
  return (
    <div
      className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex items-center gap-3 animate-scale-in"
      data-testid="error-card"
      data-continuable="true"
    >
      <div className="text-danger text-[13px] leading-5 flex-1 min-w-0" style={{ overflowWrap: 'anywhere' }}>
        {displayText}
      </div>
      <button
        type="button"
        onClick={onContinue}
        disabled={continuing}
        className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
        title={i18nT('pages.chat.errorCard.continue_hint')}
        data-testid="error-card-continue"
      >
        {continuing
          ? <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
          : <Play size={12} className="lucide-inline shrink-0" aria-hidden="true" />}
        {i18nT('pages.chat.errorCard.continue')}
      </button>
    </div>
  )
})
