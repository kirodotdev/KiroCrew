import { useId } from 'react'
import { LayoutDashboard, Sparkles, Target } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import CrewAvatar from '../../components/CrewAvatar'
import { Btn } from '../../components/ui'

/**
 * The crewmate's Dashboard tab while no page is adopted: the SUGGESTIONS page.
 *
 * A crewmate that never applied a page reads `state: "empty"` from its dashboard
 * route. The gateway still composes the default template into that body, but the
 * tab does not draw it: a page nobody chose, full of zeros, reads as the
 * crewmate's dashboard when it is not one. The tab shows this instead.
 *
 * The crewmate speaks for itself -- its own avatar under a speech bubble --
 * because the way to get a page is to ask it: it previews one with
 * `dashboard_preview` and the person applies it. The prompts under the bubble
 * are that ask, pre-written. Each only lands in the crewmate's chat box through
 * `onAct`, and the person sends it; nothing is applied from this surface.
 *
 * First shipped in #18613 against the published-panel frame, and lost when
 * #19659 deleted that frame. Ported here onto the Dynamic Dashboard.
 */

/** The prompts, by full catalog key (spelled out so the dead-key scan sees each one) and glyph. Order is the order shown. */
const PROMPTS = [
  { key: 'pages.membersPage.dashboard_empty_prompt_set_up', Icon: Sparkles },
  { key: 'pages.membersPage.dashboard_empty_prompt_choose', Icon: LayoutDashboard },
  { key: 'pages.membersPage.dashboard_empty_prompt_pick', Icon: Target },
] as const

/** Edge length of the crewmate's face. Large enough to be the scene's subject
 *  at the panel's default width, small enough that the prompts stay above the
 *  fold in a short panel. */
const AVATAR_PX = 88

export default function CrewDashboardEmpty({ member, displayName, avatar, onAct }: {
  /** The crewmate's exact name: the avatar's identity when no face is pinned. */
  member: string
  /** How the crewmate is named to the reader; the region is labelled with it so
   *  assistive tech hears WHO the bubble's "I" is. Falls back to `member`. */
  displayName?: string
  /** The crew record's `avatar` field, verbatim, as the roster row renders it. */
  avatar?: unknown
  /** Put a prompt into this crewmate's chat box; the person sends it. Without
   *  it the prompts are not shown -- a prompt that goes nowhere is a dead control. */
  onAct?: (text: string) => void
}) {
  const { t } = useTranslation()
  // The lead line is the one statement that a prompt does NOT act; every prompt
  // button is described by it so a screen reader hears the caveat with the name.
  const leadId = useId()
  return (
    <section
      className="h-full min-h-0 overflow-y-auto flex items-start justify-center p-6"
      aria-label={t('pages.membersPage.dashboard_empty_region', { name: displayName || member })}
      data-testid="crew-dashboard-suggestions"
    >
      <div className="my-auto w-full max-w-[480px] flex flex-col items-center gap-3.5">
        <div className="relative">
          <div className="rounded-2xl border border-border bg-card px-4 py-3 text-center">
            <p className="text-[14px] font-semibold text-text-strong">
              {t('pages.membersPage.dashboard_empty_bubble_title')}
            </p>
            {/* `text-text`, not muted: 13px muted on the card is under AA contrast in
                the default dark palette, and this is the sentence that says what a
                dashboard is. */}
            <p className="mt-1 text-[13px] leading-relaxed text-text">
              {t('pages.membersPage.dashboard_empty_bubble_body')}
            </p>
          </div>
          {/* The bubble's tail, pointing at the face: a rotated square whose two
              visible edges continue the bubble's border. */}
          <div
            aria-hidden="true"
            className="absolute -bottom-[7px] left-1/2 -ml-[6px] h-3 w-3 rotate-45 border-b border-r border-border bg-card"
          />
        </div>
        <CrewAvatar seed={member} avatar={avatar} size={AVATAR_PX} />
        {onAct && (
          <div className="mt-1.5 w-full" data-testid="crew-dashboard-suggestions-prompts">
            {/* THE VOICE CHANGES HERE. The bubble above is the crewmate's "I"; the
                prompts below are the PERSON's, written for them to send. A visible
                heading naming who they are addressed to, and drafts drawn as the
                person's own chat bubbles (right-aligned, a plain solid border: a dash
                read as "the one you picked" in UX review), keep
                a first-time reader from taking a prompt's "I" for the crewmate. */}
            <p className="mb-1 text-right text-[13px] font-semibold text-text-strong">
              {t('pages.membersPage.dashboard_empty_prompts_heading', { name: displayName || member })}
            </p>
            <p id={leadId} className="mb-2 text-right text-[12px] text-muted">
              {t('pages.membersPage.dashboard_empty_prompts_lead')}
            </p>
            <ul className="m-0 flex list-none flex-col items-end gap-2 p-0">
              {PROMPTS.map(({ key, Icon }) => {
                const text = t(key)
                return (
                  <li key={key} className="max-w-[92%]">
                    <Btn
                      type="button"
                      className="user-bubble w-full items-start gap-2.5 rounded-xl rounded-br-sm border-border-strong bg-card px-3 py-2.5 text-left leading-relaxed text-card-fg hover:border-accent hover:bg-card focus-visible:outline-2 focus-visible:outline-accent"
                      onClick={() => onAct(text)}
                      aria-describedby={leadId}
                      data-testid="crew-dashboard-suggestion"
                    >
                      <Icon className="lucide-inline mt-0.5 shrink-0 text-accent" aria-hidden="true" />
                      {/* No trailing glyph: the whole card is the one control, and a
                          return arrow or a pencil each read as a second target. */}
                      <span className="flex-1">{text}</span>
                    </Btn>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </div>
    </section>
  )
}
