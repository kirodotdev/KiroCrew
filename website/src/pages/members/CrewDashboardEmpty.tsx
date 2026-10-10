import { useId } from 'react'
import { ArrowRight, RefreshCw, Sparkles, Target } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import CrewAvatar from '../../components/CrewAvatar'
import { Btn } from '../../components/ui'

/**
 * The Dashboard tab before the crewmate has published anything: the state every
 * new crewmate starts in, and the tab's largest surface while it lasts.
 *
 * The crewmate speaks for itself -- its own avatar under a speech bubble -- because
 * nothing in the crew editor makes a crewmate publish. It publishes through
 * `panel_publish` when asked or on its own cycle, so the only honest next step is
 * asking it, and the bubble says so in the first person. The three prompts under it
 * are that ask, pre-written: each only lands in the member's chat box through
 * `onAct`, and the person sends it. No publish is triggered from this surface.
 *
 * The prompts are a fixed set chosen to hold for any role and to land on what the
 * default panel template can draw (a few numbers, one conclusion, an update time):
 * the first gets a dashboard at all, the second makes it keep itself current, the
 * third lets the crewmate pick what matters. Deriving them from the crewmate's
 * schedules and sessions is a follow-up; this set is the fallback that follow-up
 * will keep.
 */

/** The prompts, by full catalog key (spelled out so the dead-key scan sees each one) and glyph. Order is the order shown. */
const PROMPTS = [
  { key: 'pages.membersPage.dashboard_empty_prompt_publish', Icon: Sparkles },
  { key: 'pages.membersPage.dashboard_empty_prompt_update', Icon: RefreshCw },
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
      data-testid="crew-webview-empty"
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
          <div className="mt-1.5 w-full" data-testid="crew-webview-empty-prompts">
            <p id={leadId} className="mb-2 text-center text-[13px] text-muted">
              {t('pages.membersPage.dashboard_empty_prompts_lead')}
            </p>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {PROMPTS.map(({ key, Icon }) => {
                const text = t(key)
                return (
                  <li key={key}>
                    <Btn
                      className="w-full justify-start gap-2.5 rounded-[10px] px-3 py-2.5 text-left"
                      onClick={() => onAct(text)}
                      aria-describedby={leadId}
                      data-testid="crew-webview-empty-prompt"
                    >
                      <Icon className="lucide-inline text-accent" aria-hidden="true" />
                      <span className="flex-1">{text}</span>
                      <ArrowRight className="lucide-inline text-muted" aria-hidden="true" />
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
