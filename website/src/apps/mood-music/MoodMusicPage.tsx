/**
 * MoodMusicPage — say how you feel, hear songs with the same feeling.
 *
 * One screen: a mood input with mood chips, the DJ's one question when it has
 * one, the now-playing card and the queue; a second tab keeps the moments. The
 * page talks to the DJ agent through one hidden worker slot (`api.ts`): it sends
 * the mood plus the app's taste summary, polls the slot until the turn ends, and
 * reads the reply's `mood-music` block (`replyParse.ts`). Every link is checked
 * there and again by the backend before it is queued.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { useReducedMotion } from 'framer-motion'
import { MessageCircleQuestion, Pause, Play, Search } from 'lucide-react'
import { Btn, EmptyState, PageHeader } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { errMessage } from '../../utils/thunkError'
import { latestReply, moodApi, type MoodState, type QueuedPick, type Verdict } from './api'
import MomentsTab from './MomentsTab'
import MoodBackdrop from './MoodBackdrop'
import { MOOD_KEYS, moodLookFor, type MoodKey, type MoodLook } from './moods'
import NowPlaying from './NowPlaying'
import { buildRequest } from './request.prompt'
import { parseDjReply, replySource } from './replyParse'

type Tab = 'listen' | 'moments'

/** Poll cadence while the DJ works, and how long before the page gives up. */
export const POLL_MS = 1500
export const GIVE_UP_MS = 5 * 60_000

interface Pending {
  momentId: string
  baseline: string
  startedAt: number
}

const MOOD_LABELS = {
  calm: 'apps.moodMusic.page.mood_calm',
  sad: 'apps.moodMusic.page.mood_sad',
  happy: 'apps.moodMusic.page.mood_happy',
  missing: 'apps.moodMusic.page.mood_missing',
  fired: 'apps.moodMusic.page.mood_fired',
} as const satisfies Record<MoodKey, string>

const moodLabel = (key: MoodKey) => i18nT(MOOD_LABELS[key])

export default function MoodMusicPage() {
  const reduceMotion = !!useReducedMotion()
  const [tab, setTab] = useState<Tab>('listen')
  const [state, setState] = useState<MoodState | undefined>()
  const [text, setText] = useState('')
  const [chip, setChip] = useState<MoodKey | null>(null)
  const [answer, setAnswer] = useState('')
  const [question, setQuestion] = useState('')
  const [look, setLook] = useState<MoodLook>('neutral')
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [autoplay, setAutoplay] = useState(false)
  const [motionPaused, setMotionPaused] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const slotRef = useRef<string | null>(null)
  const momentRef = useRef<string>('')
  // Read by the poll without restarting it when a chip changes mid-turn.
  const chipRef = useRef<MoodKey | null>(null)
  chipRef.current = chip

  useEffect(() => {
    let live = true
    moodApi.state().then(s => { if (live) setState(s) }).catch(e => { if (live) setError(errMessage(e) || 'load_failed') })
    return () => { live = false }
  }, [])

  const queue = state?.queue ?? []
  const current = queue.find(p => p.id === currentId) ?? queue.find(p => p.feedback !== 'skipped') ?? null

  /** Send one turn to the DJ and start waiting for its reply. */
  const ask = useCallback(async (message: string) => {
    const known = slotRef.current ?? (await moodApi.openSlot()).key
    // The reply already in the slot, so the poll waits for a NEW one.
    const before = await moodApi.getSlot(known).catch(() => undefined)
    slotRef.current = await moodApi.ask(message)
    setPending({ momentId: momentRef.current, baseline: replySource(latestReply(before)), startedAt: Date.now() })
  }, [])

  const run = useCallback(async (work: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await work()
    } catch (e) {
      setError(errMessage(e) || 'unknown')
      setPending(null)
    } finally {
      setBusy(false)
    }
  }, [])

  const submitMood = (e: FormEvent) => {
    e.preventDefault()
    const typed = text.trim()
    const mood = typed || (chip ? moodLabel(chip) : '')
    if (!mood || pending) return
    setLook(chip ?? moodLookFor([mood]))
    setQuestion('')
    void run(async () => {
      const moment = await moodApi.addMoment(mood)
      momentRef.current = moment.id
      setState(s => (s ? { ...s, moments: [moment, ...s.moments] } : s))
      await ask(buildRequest({ kind: 'mood', text: mood, state }))
    })
  }

  const submitAnswer = (e: FormEvent) => {
    e.preventDefault()
    const reply = answer.trim()
    if (!reply || pending) return
    setQuestion('')
    setAnswer('')
    void run(() => ask(buildRequest({ kind: 'answer', text: reply, state })))
  }

  const moreLike = (pick: QueuedPick) => {
    if (pending) return
    const song = pick.artist ? `"${pick.title}" - ${pick.artist}` : `"${pick.title}"`
    void run(() => ask(buildRequest({ kind: 'more', text: song, state })))
  }

  // Poll the slot while a turn is out. Ends on evidence (a new reply, turn over)
  // or after GIVE_UP_MS.
  useEffect(() => {
    if (!pending) return
    let live = true
    const timer = setInterval(async () => {
      const key = slotRef.current
      if (!key || !live) return
      if (Date.now() - pending.startedAt > GIVE_UP_MS) {
        setPending(null)
        setError('timeout')
        return
      }
      let slot
      try {
        slot = await moodApi.getSlot(key)
      } catch {
        return // a transient blip; the next tick tries again
      }
      const reply = latestReply(slot)
      const source = replySource(reply)
      if (!live || slot.running || !reply || source === pending.baseline) return
      live = false
      setPending(null)
      const parsed = parseDjReply(reply)
      if (!parsed) {
        setError('unreadable')
        return
      }
      const tagLook = moodLookFor(parsed.moodTags)
      if (!chipRef.current && tagLook !== 'neutral') setLook(tagLook)
      if (parsed.picks.length === 0) {
        if (parsed.question) setQuestion(parsed.question)
        else setError('unreadable')
        return
      }
      if (parsed.dropped > 0) setNotice(i18nT('apps.moodMusic.page.dropped', { count: parsed.dropped }))
      try {
        const { queue: next } = await moodApi.setQueue({
          source, moment_id: pending.momentId || undefined, mood_tags: parsed.moodTags, picks: parsed.picks,
        })
        setState(await moodApi.state())
        setCurrentId(next[0]?.id ?? null)
        setAutoplay(false)
      } catch (e) {
        setError(errMessage(e) || 'unknown')
      }
    }, POLL_MS)
    return () => { live = false; clearInterval(timer) }
  }, [pending])

  const giveFeedback = (pick: QueuedPick, verdict: Verdict) => {
    void run(async () => {
      const updated = await moodApi.feedback(pick.id, verdict)
      setState(await moodApi.state().catch(() => state))
      if (verdict === 'skipped') {
        const rest = queue.filter(p => p.id !== updated.id && p.feedback !== 'skipped')
        setCurrentId(rest[0]?.id ?? null)
        setAutoplay(true)
      }
    })
  }

  const playPick = (pick: QueuedPick) => {
    setCurrentId(pick.id)
    setAutoplay(true)
  }

  const errorText = error === null ? null
    : error === 'unreadable' ? i18nT('apps.moodMusic.page.error_unreadable')
      : error === 'timeout' ? i18nT('apps.moodMusic.page.error_timeout')
        : i18nT('apps.moodMusic.page.error_generic', { code: error })
  const working = busy || !!pending
  const moving = !reduceMotion && !motionPaused

  return (
    <div className="relative flex h-full flex-col overflow-hidden" data-testid="mood-music-page">
      <MoodBackdrop look={look} moving={moving} />
      <div className="relative flex h-full flex-col">
        <PageHeader
          title={i18nT('apps.moodMusic.page.title')}
          subtitle={i18nT('apps.moodMusic.page.subtitle')}
          actions={reduceMotion ? undefined : (
            <Btn
              onClick={() => setMotionPaused(p => !p)}
              aria-pressed={motionPaused}
              aria-label={motionPaused ? i18nT('apps.moodMusic.page.play_motion') : i18nT('apps.moodMusic.page.pause_motion')}
              title={motionPaused ? i18nT('apps.moodMusic.page.play_motion') : i18nT('apps.moodMusic.page.pause_motion')}
              data-testid="mood-motion-toggle"
            >
              {motionPaused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
            </Btn>
          )}
        />
        <div className="px-4 pb-2">
          <SegmentedControl
            value={tab}
            onChange={v => setTab(v)}
            segments={[
              { key: 'listen', label: i18nT('apps.moodMusic.page.tab_listen') },
              { key: 'moments', label: i18nT('apps.moodMusic.page.tab_moments') },
            ]}
          />
        </div>

        <div className="flex-1 overflow-y-auto px-4 pb-6">
          {tab === 'moments' ? <div className="mx-auto max-w-5xl"><MomentsTab moments={state?.moments ?? []} /></div> : (
            <div className="mx-auto flex max-w-5xl flex-col gap-4">
              <form onSubmit={submitMood} className="rounded-2xl border border-[var(--border)] bg-[color-mix(in_oklab,var(--card)_82%,transparent)] p-4 shadow-sm backdrop-blur-md">
                <label id="mood-input-label" htmlFor="mood-input" className="text-sm font-medium text-[var(--text)]">
                  {i18nT('apps.moodMusic.page.mood_label')}
                </label>
                <div className="mt-2 flex gap-2">
                  <input
                    id="mood-input"
                    aria-labelledby="mood-input-label"
                    value={text}
                    onChange={e => setText(e.target.value)}
                    maxLength={200}
                    placeholder={i18nT('apps.moodMusic.page.mood_placeholder')}
                    className="min-w-0 flex-1 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  />
                  <Btn type="submit" primary disabled={working || (!text.trim() && !chip)} busy={working}>
                    <Search size={14} aria-hidden="true" />
                    {i18nT('apps.moodMusic.page.find')}
                  </Btn>
                </div>
                <div role="group" aria-label={i18nT('apps.moodMusic.page.mood_chips')} className="mt-3 flex flex-wrap gap-2">
                  {MOOD_KEYS.map(key => (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={chip === key}
                      onClick={() => { setChip(c => (c === key ? null : key)); setLook(chip === key ? 'neutral' : key) }}
                      className={`rounded-full border px-3 py-1 text-xs transition-colors ${chip === key
                        ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-semibold text-[var(--accent)]'
                        : 'border-[var(--border)] text-[var(--text)] hover:bg-[var(--bg-hover)]'}`}
                    >
                      {moodLabel(key)}
                    </button>
                  ))}
                </div>
                {working && (
                  <p role="status" className="mt-3 text-xs text-[var(--muted)]">{i18nT('apps.moodMusic.page.finding')}</p>
                )}
                <ErrorNotice className="mt-2" message={errorText} />
                {notice && <p className="mt-2 text-xs text-[var(--muted)]">{notice}</p>}
              </form>

              {question && (
                <form onSubmit={submitAnswer} className="rounded-2xl border border-[var(--accent)] bg-[color-mix(in_oklab,var(--card)_82%,transparent)] p-4 backdrop-blur-md" data-testid="mood-question">
                  <p className="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]">
                    <MessageCircleQuestion size={14} aria-hidden="true" />
                    {i18nT('apps.moodMusic.page.question_label')}
                  </p>
                  <p className="mt-1 text-sm text-[var(--text)]">{question}</p>
                  <div className="mt-2 flex gap-2">
                    <input
                      aria-label={i18nT('apps.moodMusic.page.answer_placeholder')}
                      value={answer}
                      onChange={e => setAnswer(e.target.value)}
                      maxLength={300}
                      placeholder={i18nT('apps.moodMusic.page.answer_placeholder')}
                      className="min-w-0 flex-1 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                    />
                    <Btn type="submit" primary disabled={working || !answer.trim()}>
                      {i18nT('apps.moodMusic.page.send_answer')}
                    </Btn>
                  </div>
                </form>
              )}

              <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
                {current ? (
                  <NowPlaying
                    pick={current}
                    autoplay={autoplay}
                    busy={working}
                    onLike={() => giveFeedback(current, current.feedback === 'liked' ? '' : 'liked')}
                    onSkip={() => giveFeedback(current, 'skipped')}
                    onMore={() => moreLike(current)}
                  />
                ) : (
                  <EmptyState
                    icon={<Play size={28} />}
                    title={i18nT('apps.moodMusic.page.empty_title')}
                    subtitle={i18nT('apps.moodMusic.page.empty_body')}
                  />
                )}

                {queue.length > 1 && (
                  <section aria-label={i18nT('apps.moodMusic.page.queue')}>
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">{i18nT('apps.moodMusic.page.queue')}</h3>
                    <ul className="flex flex-col gap-1.5" data-testid="mood-queue">
                      {queue.map(p => (
                        <li key={p.id}>
                          <button
                            type="button"
                            onClick={() => playPick(p)}
                            aria-current={current?.id === p.id ? 'true' : undefined}
                            aria-label={i18nT('apps.moodMusic.page.play', { title: p.title })}
                            className={`flex w-full items-center gap-3 rounded-xl border px-3 py-2 text-left transition-colors ${current?.id === p.id
                              ? 'border-[var(--accent)] bg-[var(--accent-subtle)]'
                              : 'border-[var(--border)] bg-[color-mix(in_oklab,var(--card)_70%,transparent)] hover:bg-[var(--bg-hover)]'} ${p.feedback === 'skipped' ? 'opacity-50' : ''}`}
                          >
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm text-[var(--text)]">{p.title}</span>
                              <span className="block truncate text-xs text-[var(--muted)]">{p.artist}</span>
                            </span>
                            <span className="shrink-0 rounded-full border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)]">
                              {p.source === 'youtube' ? i18nT('apps.moodMusic.page.source_youtube') : i18nT('apps.moodMusic.page.source_bilibili')}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
