import { useState } from 'react'
import {
  Activity, Clock, Code2, Eye, FileText, ListChecks, RefreshCw, Search, Sparkles, type LucideIcon,
} from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KiroGhost } from './KiroGhost'
import { useTheme } from '../hooks/useTheme'
import { getThemeBranding } from '../themeBranding'
import { api, type SuggestionItem, type SuggestionKind } from '../api/client'

import { i18nT } from '../i18n/t'
interface WelcomeViewProps {
  setInput: (v: string) => void
}

/** Icon and tint per suggestion kind. */
export const SUGGESTION_KIND_STYLE: Record<SuggestionKind, { Icon: LucideIcon; tile: string }> = {
  code: { Icon: Code2, tile: 'bg-accent-subtle text-accent' },
  review: { Icon: Eye, tile: 'bg-info-subtle text-info' },
  ops: { Icon: Activity, tile: 'bg-danger-subtle text-danger' },
  tasks: { Icon: ListChecks, tile: 'bg-ok-subtle text-ok' },
  write: { Icon: FileText, tile: 'bg-warn-subtle text-warn' },
  research: { Icon: Search, tile: 'bg-info-subtle text-info' },
  schedule: { Icon: Clock, tile: 'bg-accent-subtle text-accent' },
  general: { Icon: Sparkles, tile: 'bg-accent-subtle text-accent' },
}

export interface Suggestion { text: string; kind: SuggestionKind }

/** Accept a legacy bare string or `{ text, kind }`; an unknown or missing kind becomes `general`. */
export function normalizeSuggestion(item: SuggestionItem): Suggestion {
  if (typeof item === 'string') return { text: item, kind: 'general' }
  const kind = item.kind && Object.hasOwn(SUGGESTION_KIND_STYLE, item.kind) ? item.kind as SuggestionKind : 'general'
  return { text: item.text, kind }
}

/** Greeting catalog keys the heading picks from; the last one follows the local hour. */
export function welcomeGreetingKeys(hour: number = new Date().getHours()): string[] {
  const timeOfDay = hour < 12
    ? 'components.welcomeView.greeting_good_morning'
    : hour < 18 ? 'components.welcomeView.greeting_good_afternoon' : 'components.welcomeView.greeting_good_evening'
  return [
    'components.welcomeView.what_can_i_do_for_you',
    'components.welcomeView.greeting_what_are_we_building_today',
    'components.welcomeView.greeting_where_should_we_start',
    'components.welcomeView.greeting_whats_on_your_mind',
    'components.welcomeView.greeting_ready_when_you_are',
    'components.welcomeView.greeting_good_to_see_you',
    timeOfDay,
  ]
}

/** Every greeting the heading can show in the active language, for tests that must not pin one. */
export function welcomeGreetings(): string[] {
  const keys = [0, 12, 18].flatMap(h => welcomeGreetingKeys(h))
  return [...new Set(keys)].map(k => i18nT(k))
}

function SuggestedCards({ setInput }: { setInput: (v: string) => void }) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const { data, isFetching } = useQuery({
    queryKey: ['suggestions'],
    queryFn: () => api.suggestions(),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    refetchOnWindowFocus: false,
  })

  // Built at render (not module scope) so each i18nT() reads the active language;
  // the App remounts on a language switch, re-evaluating these.
  const fallbackSuggestions: Suggestion[] = [
    { text: i18nT('components.welcomeView.suggestion_pipeline_status'), kind: 'ops' },
    { text: i18nT('components.welcomeView.suggestion_triage_tickets'), kind: 'tasks' },
    { text: i18nT('components.welcomeView.suggestion_search_code'), kind: 'research' },
    { text: i18nT('components.welcomeView.suggestion_summarize_chat'), kind: 'general' },
    { text: i18nT('components.welcomeView.suggestion_write_design_doc'), kind: 'write' },
    { text: i18nT('components.welcomeView.suggestion_review_cr'), kind: 'review' },
  ]
  const cards = data?.suggestions?.length ? data.suggestions.map(normalizeSuggestion) : fallbackSuggestions

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await api.suggestions(true)
      qc.setQueryData(['suggestions'], fresh)
    } catch {}
    setRefreshing(false)
  }

  const spinning = isFetching || refreshing

  return (
    // `isolate` keeps the z-10 / z-20 below inside this box. The composer dock
    // floats over the hero with no z-index of its own (ChatPage explains why) and
    // wins only by DOM order, so an escaped z-20 put the Refresh row on top of
    // the composer's mic and Send buttons whenever the hero scrolled under it.
    <div data-testid="welcome-suggestions" className="isolate w-full max-w-[620px] mx-auto">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {cards.map(({ text, kind }, i) => {
          const { Icon, tile } = SUGGESTION_KIND_STYLE[kind]
          return (
            // Phones: a compact one-line row. sm+: fixed-height cell with the card absolute
            // inside it, so on hover/focus it grows DOWN over the next row instead of reflowing.
            <div key={`${i}-${text}`} className="relative sm:h-[108px] hover:z-10 focus-within:z-10">
              {/* type=button + onMouseDown preventDefault stop the card from taking
                  keyboard focus on click. Without this the focused card is re-activated
                  by a follow-up Enter (re-firing setInput) instead of submitting via the
                  textarea, so the prompt appears to clear instead of send. */}
              <button
                type="button"
                data-kind={kind}
                onMouseDown={e => e.preventDefault()}
                onClick={() => setInput(text)}
                className="group relative w-full sm:absolute sm:top-0 sm:inset-x-0 sm:min-h-full flex flex-row sm:flex-col items-center sm:items-start gap-3 px-4 py-3 sm:gap-2.5 sm:p-3.5 rounded-xl border border-border bg-card text-card-fg text-[13px] font-medium text-left overflow-hidden cursor-pointer transition-[border-color,box-shadow] duration-200 hover:border-accent hover:shadow-lg focus-visible:border-accent focus-visible:shadow-lg"
              >
                <span aria-hidden="true" className={`w-8 h-8 shrink-0 rounded-lg flex items-center justify-center ${tile}`}>
                  <Icon size={16} />
                </span>
                <span className="min-w-0 leading-[1.35] truncate sm:whitespace-normal sm:line-clamp-2 sm:group-hover:line-clamp-none sm:group-focus-visible:line-clamp-none">{text}</span>
              </button>
            </div>
          )
        })}
      </div>
      {/* z-20 keeps the link above a hovered bottom-row card (z-10) growing over it;
          `isolate` on the root keeps both from competing with the composer dock. */}
      <div className="relative z-20 flex justify-end mt-4">
        <button
          type="button"
          onClick={handleRefresh}
          disabled={spinning}
          className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[12px] text-muted hover:text-text hover:bg-bg-hover bg-transparent transition-colors cursor-pointer disabled:cursor-default"
        >
          <RefreshCw size={12} className={spinning ? 'animate-spin' : ''} />
          <span>{i18nT('components.welcomeView.refresh_suggestions')}</span>
        </button>
      </div>
    </div>
  )
}

export default function WelcomeView({ setInput }: WelcomeViewProps) {
  // One greeting per mount: the KEY is fixed here so a re-render never reshuffles,
  // while i18nT below still re-resolves it on a language switch.
  const [greetingKey] = useState(() => {
    const keys = welcomeGreetingKeys()
    return keys[Math.floor(Math.random() * keys.length)] ?? keys[0]
  })

  // Per-theme brand mark: a registered theme (via the themeBranding seam) may
  // supply its own logo — render it here too, not just in the App shell, so the
  // welcome screen matches the active theme. Falls back to the stock KiroGhost
  // when the theme registers no logo (the standalone build always does).
  const { colorTheme } = useTheme()
  const brandLogo = getThemeBranding(colorTheme)?.logo
  const brandMark = brandLogo
    ? <img src={brandLogo} alt="" aria-hidden="true" className="w-12 h-12 drop-shadow-lg shrink-0 animate-float rounded-md object-contain" />
    : <KiroGhost size={48} className="drop-shadow-lg shrink-0 animate-float" />

  // The memory chip sits above the composer (ChatPage renders it).
  return (
    // Unprefixed = phones: a safe-centred column (falls back to top-aligned when it
    // overflows, so nothing clips). Short wide windows stack from the top. Wide and tall
    // viewports switch to the spread grid: greeting ~20% down, cards ~60%.
    // No `min-h-0`: the column must grow to its content so the hero scroller's
    // bottom padding (the dock's height) lands after the Refresh row. A column
    // shrunk to the hero let that row spill past the padding, where no scroll
    // could lift it clear of the composer.
    <div data-testid="welcome-layout" className="w-full flex-1 pt-12 pb-4 flex flex-col [justify-content:safe_center] sm:justify-start gap-6 [@media(min-width:640px)_and_(min-height:600px)]:pt-0 [@media(min-width:640px)_and_(min-height:600px)]:pb-0 [@media(min-width:640px)_and_(min-height:600px)]:grid [@media(min-width:640px)_and_(min-height:600px)]:grid-cols-1 [@media(min-width:640px)_and_(min-height:600px)]:grid-rows-[1.3fr_auto_0.7fr] [@media(min-width:640px)_and_(min-height:600px)]:gap-0">
      <div className="flex flex-col items-center w-full shrink-0 min-h-0">
        <div aria-hidden="true" className="hidden basis-[45%] shrink min-h-4 [@media(min-width:640px)_and_(min-height:600px)]:block" />
        <div className="flex flex-col items-center gap-3 text-center shrink-0">
          {brandMark}
          <h2 className="text-3xl sm:text-4xl font-light text-text-strong tracking-tight">{i18nT(greetingKey)}</h2>
        </div>
        <div aria-hidden="true" className="hidden grow min-h-8 [@media(min-width:640px)_and_(min-height:600px)]:block" />
      </div>
      <SuggestedCards setInput={setInput} />
      <div aria-hidden="true" className="hidden [@media(min-width:640px)_and_(min-height:600px)]:block" />
    </div>
  )
}
