import { i18nT } from '../../i18n/t'

/**
 * The prompts a Bot's owner pinned to its empty chat, as buttons. A press sends the
 * prompt as the person's first message, the way the follow-up chips do. The text is
 * the owner's own and already bounded by the gateway (up to four, one line each), so
 * it is drawn as plain text. The buttons stand one per row, a stack rather than a row
 * of peers, so short prompts never sit more than one side by side.
 */
export default function CrewmateStarters({ starters, onPick }: {
  starters: string[]
  onPick: (text: string) => void
}) {
  return (
    <div
      role="group"
      aria-label={i18nT('components.chatPane.starters_label')}
      data-testid="crewmate-starters"
      className="mx-auto flex w-full max-w-[34rem] flex-col items-stretch gap-2 px-4 py-6"
    >
      {starters.map((text) => (
        <button
          key={text}
          type="button"
          onClick={() => onPick(text)}
          data-testid="crewmate-starter"
          className="w-full rounded-2xl border border-border bg-card px-3.5 py-2 text-left text-[13px] leading-snug text-text transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent cursor-pointer break-words"
        >
          {text}
        </button>
      ))}
    </div>
  )
}
