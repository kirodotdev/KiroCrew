import { useState, useEffect, useRef, type ReactNode } from 'react'
import {
  PencilRuler, Image as ImageIcon, Upload, Plus, ChevronDown,
  Check, ChevronRight, Maximize2, X, Sparkle,
} from 'lucide-react'

import { sevOf, KIND_LABEL, HARD_CAP_MS, MAX_SCREENS, BLOCKED, SAMPLE_REPORT, SAMPLE_SCREENS } from './constants'
import Clickable from '../../components/Clickable'
import { Spinner } from './Motion'
import { S } from './styles'
import { designCritiqueApi, fileUrl } from './api'
import {
  detectKind, extractJson, lastAssistant, shortLabel, relTime, readableOn, normalizeReport, normalizeScope,
  loadHistory, saveHistory, beginPendingCritique, dropPendingCritique, loadJobs, saveJob, clearJob, loadSlots, saveSlots, trackSlot, untrackSlot,
  loadLive, markLive, unmarkLive,
} from './utils'
import { IMAGES_PROMPT, DISCOVER_PROMPT, SCOPED_PROMPT, ASK_CONTEXT, ASK_PROMPT } from './prompts'
import { useReduceMotion, useNarrow, useToasts } from './hooks'
import FindingRow from './FindingRow'
import WaitingScreen from './WaitingScreen'
import Composer from './Composer'
import ScopingPicker from './ScopingPicker'
import AskLayer from './AskLayer'
import type {
  Ask,
  Blocked,
  BlockedInfo,
  DiscoveryScreen,
  Finding,
  Flow,
  HistoryEntry,
  Job,
  Phase,
  Report,
  Scope,
  Screen,
  Sel,
  StagedItem,
} from './types'

import { i18nT } from '../../i18n/t'
// Raw discovery JSON (STEP 1) before it's filtered into a Scope.
interface DiscoveryInfo {
  framework?: string
  note?: string
  blocked?: BlockedInfo | null
  screens?: DiscoveryScreen[]
  flows?: Flow[]
  cannotSee?: string[]
}

// Errors flagged with why the poll loop gave up — distinguishes navigate-away and
// timeout (both resumable) from a real failure.
type Flagged = Error & { cancelled?: boolean; timeout?: boolean }
const CANCELLED = (): Flagged => Object.assign(new Error('cancelled'), { cancelled: true })
const TIMEOUT = (): Flagged => Object.assign(new Error('still running'), { timeout: true })

export default function DesignCritiquePage() {
  const [phase, setPhase] = useState<Phase>('new')
  const [scope, setScope] = useState<Scope | null>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [refBrief, setRefBrief] = useState('')
  const [slot, setSlot] = useState('')
  // slotKey is carried so the chip resolves the entry belonging to THIS run:
  // a second critique finishing first takes history index 0, and annotating
  // through the chip would then write onto the wrong critique's entry.
  const [justFinished, setJustFinished] = useState<{ slotKey: string; read: string; screens: Screen[]; report: Report } | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)
  const [sel, setSel] = useState<Sel | null>(null)
  const [asks, setAsks] = useState<Ask[]>([])
  const [openAskId, setOpenAskId] = useState<string | null>(null)
  const [askDraft, setAskDraft] = useState('')
  const [blocked, setBlocked] = useState<Blocked | null>(null)
  const [showAuth, setShowAuth] = useState(false)
  const [current, setCurrent] = useState<{ report: Report | null; screens: Screen[]; entryId?: number | null } | null>(null)
  const [critiques, setCritiques] = useState<HistoryEntry[]>(loadHistory)
  const [err, setErr] = useState('')
  const [dragging, setDragging] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [zoom, setZoom] = useState(false)
  const [active, setActive] = useState<number | null>(null)
  const [open, setOpen] = useState<Set<number>>(() => new Set([0]))
  const [screenIdx, setScreenIdx] = useState(0)
  const [refText, setRefText] = useState('')
  const [staged, setStaged] = useState<StagedItem[]>([])
  const [elapsed, setElapsed] = useState(0)
  const [writing, setWriting] = useState(false)
  const [pendingKind, setPendingKind] = useState<string | null>(null)

  const reduceMotion = useReduceMotion()
  const { toasts, notify } = useToasts()

  const inputRef = useRef<HTMLInputElement>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const startedAtRef = useRef(0)
  const aliveRef = useRef(true)
  const phaseRef = useRef<Phase>('new')
  const activeSlotRef = useRef('')
  /**
   * Slots whose run the user cancelled. A single boolean was wrong once more than
   * one critique could be in flight: cancelling either run tripped the flag every
   * poller checks, so both exited and the other run's result was discarded.
   */
  const cancelledRef = useRef<Set<string>>(new Set())
  /**
   * Bumped whenever the user starts or resets a run, so an async step that began
   * earlier can tell it no longer owns the screen. An upload has no slot yet —
   * the slot is created after it completes — so `activeSlotRef` cannot identify
   * it and `isWatching()` has nothing to compare; this counter is what protects
   * the pre-slot window.
   */
  const runSeqRef = useRef(0)
  /** Slots that already have a live poller, so a run is never polled twice. */
  const pollingRef = useRef<Set<string>>(new Set())
  const isCancelled = (slotKey: string): boolean => cancelledRef.current.has(slotKey)
  const askSlotRef = useRef('')
  /**
   * Follow-ups deliberately share ONE chat slot so the thread keeps its context.
   * That makes them strictly sequential: `/api/chat` queues a second turn on the
   * same slot and `pollForText` only ever reads the latest assistant message, so
   * two turns in flight both resolve to the second answer and the first question
   * is recorded with a reply that does not belong to it.
   */
  const [askPending, setAskPending] = useState(false)
  const threadRef = useRef<HTMLDivElement>(null)

  const narrow = useNarrow(rootRef)

  // Must re-arm on setup, not only clear on teardown: StrictMode mounts, unmounts
  // and remounts effects, so a teardown-only version left aliveRef false forever
  // and every poll cancelled itself.
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])
  useEffect(() => { phaseRef.current = phase }, [phase])

  // Real elapsed time while a run is in flight. Derived from a START TIMESTAMP, not
  // by counting ticks — counting is wrong if two intervals ever overlap.
  useEffect(() => {
    if (phase !== 'uploading' && phase !== 'analyzing' && phase !== 'scanning') return
    const tick = () => {
      const t0 = startedAtRef.current
      setElapsed(t0 ? Math.max(0, Math.floor((Date.now() - t0) / 1000)) : 0)
    }
    tick()
    const id = setInterval(tick, 500)
    return () => clearInterval(id)
  }, [phase])

  useEffect(() => {
    if (!zoom) return
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') setZoom(false) }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [zoom])

  // ── poll loops ──────────────────────────────────────────────────────────
  // Ends on EVIDENCE, not a clock; the runaway backstop is measured against
  // Date.now(), never a loop count. Bails the moment this view goes away.
  const pollForReport = async <T,>(slotKey: string): Promise<T> => {
    const began = Date.now()
    let misses = 0
    while (Date.now() - began < HARD_CAP_MS) {
      await new Promise(r => setTimeout(r, 1500))
      if (!aliveRef.current || isCancelled(slotKey)) throw CANCELLED()
      let d
      try { d = await designCritiqueApi.getSlot(slotKey) }
      catch {
        if (++misses >= 8) throw new Error('That run is no longer available — start a new critique.')
        continue
      }
      misses = 0
      if (!aliveRef.current || isCancelled(slotKey)) throw CANCELLED()
      const c = lastAssistant(d && d.messages)
      if (c && c.trim() && activeSlotRef.current === slotKey) setWriting(true)
      if (d && !d.running && c) { const p = extractJson<T>(c); if (p) return p; throw new Error('The critic replied but not in a readable format.') }
    }
    throw TIMEOUT()
  }

  // Follow-up answers are prose, not JSON — so this waits for text, not a schema.
  const pollForText = async (slotKey: string): Promise<string> => {
    const began = Date.now()
    let misses = 0
    while (Date.now() - began < 3 * 60 * 1000) {
      await new Promise(r => setTimeout(r, 1200))
      if (!aliveRef.current) throw CANCELLED()
      let d
      try { d = await designCritiqueApi.getSlot(slotKey) }
      catch { if (++misses >= 8) throw new Error('lost contact'); continue }
      misses = 0
      const c = lastAssistant(d && d.messages)
      if (d && !d.running && c && c.trim()) return c.trim()
    }
    throw TIMEOUT()
  }

  const startClock = (fromMs?: number) => { startedAtRef.current = fromMs || Date.now(); setElapsed(0) }

  const openSlot = async (): Promise<string> => {
    const s = await designCritiqueApi.openSlot()
    trackSlot(s.key); markLive(s.key)
    return s.key
  }
  const send = (slotKey: string, message: string) => designCritiqueApi.send(slotKey, message)
  const dropSlot = (slotKey: string) => { if (!slotKey) return; untrackSlot(slotKey); unmarkLive(slotKey); designCritiqueApi.deleteSlot(slotKey) }

  const showReport = (raw: Report, screens: Screen[], entry?: HistoryEntry) => {
    // Also normalise on the way IN, not just on the way out of a run: entries
    // written to dc-history-v1 before this guard existed can still hold a
    // wrong-shaped field, and reopening one would otherwise crash on every visit.
    const report = normalizeReport(raw) || raw
    setCurrent({ report, screens: screens || [], entryId: entry ? entry.id : null })
    const saved = (entry && Array.isArray(entry.asks) ? entry.asks : []).map(
      (a: Ask & { q?: string; a?: string; failed?: boolean }) =>
        a.turns ? a : { id: a.id, quote: a.quote, turns: [{ t: 0, q: a.q || '', a: a.a || '', pending: false, failed: !!a.failed }] })
    setAsks(saved)
    setOpenAskId(null); setSel(null); setAskDraft('')
    if (askSlotRef.current) { dropSlot(askSlotRef.current); askSlotRef.current = '' }
    setOpen(new Set([0])); setActive(null); setZoom(false); setScreenIdx(0); setPhase('report')
  }

  // Prefer the critic's own rendered screens; fall back to whatever we uploaded.
  const resolveScreens = (rep: Report, uploaded: Screen[]): Screen[] => {
    const fromRep = Array.isArray(rep && rep.screens) ? rep.screens!.filter(s => s && s.path) : []
    if (fromRep.length) {
      return fromRep.map((s, i) => ({ step: s.step || i + 1, label: s.label || 'Screen ' + (i + 1), url: fileUrl(s.path!) }))
    }
    return (uploaded || []).map((u, i) => ({ step: i + 1, label: (rep && rep.screens && rep.screens[i] && rep.screens[i].label) || 'Screen ' + (i + 1), url: u.url }))
  }

  /**
   * Clear the persisted job ONLY if it is this run's.
   *
   * One job record is persisted at a time, so an older run reaching its end must
   * not wipe a newer one's. Start critique A, start B while A is still going, let
   * A finish: unconditional clearing dropped B's record, and the mount-time
   * reaper then treated B's slot as a stray and deleted the run with it.
   * Records are keyed by slot now, so this removes exactly this run's entry
   * and leaves any other in-flight run resumable.
   */
  /**
   * End one run completely: forget its resume pointer, release its server slot,
   * and remove any pending row. Releasing the slot WITHOUT forgetting the job has
   * now been a defect three separate times (start-over, cancel, terminal
   * discovery) — a reload then resumed a run whose slot no longer exists and died
   * on an availability error. Every terminal path goes through here so the two
   * cannot drift apart again.
   */
  const endRun = (slotKey: string) => {
    // An empty key would reach clearJob()'s clear-all branch and delete every
    // persisted run, so a failure before the slot exists is a no-op.
    if (!slotKey) return
    clearJob(slotKey)
    dropSlot(slotKey)
    setCritiques(dropPendingCritique(slotKey))
  }

  /**
   * Is the user currently watching this run? Both the run slot AND a waiting
   * phase must match: more than one critique can be in flight, so a run that
   * has been backgrounded by `+ New` must never write to the foreground.
   */
  const isWatching = (slotKey: string): boolean =>
    activeSlotRef.current === slotKey &&
    (phaseRef.current === 'analyzing' || phaseRef.current === 'uploading' || phaseRef.current === 'scanning')

  const finishReport = (slotKey: string, uploaded: Screen[], raw: Report) => {
    // Normalise the model's JSON once, here, before it reaches history or render.
    // Every caller funnels through this, so no render site can be handed a
    // non-array for keep / couldNotSee / findings / steps / rules.
    const rep = normalizeReport(raw) || raw
    const screens = resolveScreens(rep, uploaded)
    const cur = loadHistory(); let next = cur
    const at = cur.findIndex(e => e.slotKey === slotKey)
    const thumbUrl = screens[0] ? screens[0].url : ''
    const read = rep.overallRead || ''
    if (at < 0) {
      next = [{ id: Date.now(), ts: Date.now(), slotKey, screens, thumbUrl, read, report: rep }, ...cur]
      saveHistory(next)
    } else if (cur[at].pending) {
      // Backgrounded run finishing: fill the placeholder rather than adding a
      // second row, and keep its id and position so the list does not reshuffle.
      next = cur.slice()
      next[at] = { ...cur[at], screens, thumbUrl, read, report: rep, pending: false }
      saveHistory(next)
    }
    endRun(slotKey)
    setCritiques(next)
    // Take over the screen only if the user is waiting for THIS run. Testing the
    // phase alone was not enough: starting a second critique puts the phase back
    // to 'analyzing', so the first run read that as "still waiting for me" and
    // replaced the second run's in-progress view with its own report. Any run the
    // user is no longer watching announces itself with the ready chip instead.
    const waiting = isWatching(slotKey)
    if (waiting) showReport(rep, screens, next.find(e => e.slotKey === slotKey) || next[0])
    else setJustFinished({ slotKey, read: rep.overallRead || 'Critique ready', screens, report: rep })
  }

  const failWith = (e: unknown, slotKey: string) => {
    const flag = e as Flagged
    if (flag && flag.cancelled) return
    // Same rule as finishReport: only the run on screen may change the screen.
    // A background run that fails or times out reports through the notification,
    // never by dropping the foreground run into an error state.
    const watching = isWatching(slotKey)
    if (flag && flag.timeout) {
      if (watching) { setErr('Still working on this one. It’s kept running — come back in a minute.'); setPhase('error') }
      return
    }
    endRun(slotKey)
    setCritiques(dropPendingCritique(slotKey))
    if (watching) { setErr(e instanceof Error ? e.message : 'something went wrong'); setPhase('error') }
    notify('Critique failed: ' + (e instanceof Error ? e.message : String(e)), { type: 'error' })
  }

  const ask = async (prompt: string, uploaded: Screen[], foreground = true) => {
    let slotKey = ''
    try {
      slotKey = await openSlot()
      // Claiming activeSlotRef is what makes a run the foreground one, so a
      // superseded upload must not claim it — it would redirect every guard that
      // keys off the active slot to a run the user is no longer looking at.
      if (foreground) activeSlotRef.current = slotKey
      else setCritiques(beginPendingCritique(slotKey, uploaded || []))
      saveJob({ stage: 'analyzing', slotKey, screens: uploaded || [], ts: Date.now() })
      await send(slotKey, prompt)
      const rep = await pollForReport<Report>(slotKey)
      finishReport(slotKey, uploaded, rep)
    } catch (e) { failWith(e, slotKey) }
  }

  // One or many screenshots. Order is the order you gave them.
  const runImages = async (fileList: File[]) => {
    const files = Array.from(fileList || []).filter(f => /^image\//.test(f.type || ''))
    if (!files.length) { setErr('Those weren’t image files.'); setPhase('error'); return }
    if (files.length > 20) { setErr('That’s more than 20 screens — send fewer.'); setPhase('error'); return }
    const seq = ++runSeqRef.current
    setErr(''); setBlocked(null); setShowAuth(false); setMenuOpen(false); startClock(); setWriting(false); setPendingKind(null); setPhase('uploading')
    try {
      const { paths } = await designCritiqueApi.uploadFiles(files)
      if (!paths || !paths.length) throw new Error('no file paths returned')
      const uploaded = paths.map((p, i) => ({ step: i + 1, label: 'Screen ' + (i + 1), url: fileUrl(p) }))
      // If `+ New` ran while this upload was in flight, a later run owns the
      // screen: keep critiquing in the background rather than stealing it back.
      const mine = runSeqRef.current === seq
      if (mine) { setCurrent({ report: null, screens: uploaded }); setScreenIdx(0); setPhase('analyzing') }
      await ask(IMAGES_PROMPT(paths), uploaded, mine)
    } catch (e) {
      // No slot exists yet at this point; ask() owns cleanup for the one it creates.
      if (runSeqRef.current === seq) { setErr(e instanceof Error ? e.message : 'something went wrong'); setPhase('error') }
    }
  }

  // A Figma link, a repo, or a local package. Step 1: find out what's in there.
  const runRef = async (raw: string) => {
    const det = detectKind(raw)
    if (!det) return
    if (det.kind === 'unknown') {
      setErr('I couldn’t tell what that is. Give me a Figma link, a GitHub/GitLab/Bitbucket repo, an absolute local path, or a URL that’s already serving.')
      setPhase('error'); return
    }
    setErr(''); setBlocked(null); setShowAuth(false); setMenuOpen(false); startClock(); setWriting(false); setPendingKind(det.kind)
    setCurrent({ report: null, screens: [] }); setScreenIdx(0); setScope(null); setPicked([])
    setPhase('scanning')
    let slotKey = ''
    try {
      slotKey = await openSlot()
      activeSlotRef.current = slotKey; setSlot(slotKey)
      saveJob({ stage: 'scanning', slotKey, kind: det.kind, value: det.value, ts: Date.now() })
      await send(slotKey, DISCOVER_PROMPT(det.kind, det.value))
      const info = await pollForReport<DiscoveryInfo>(slotKey)
      const list = Array.isArray(info.screens) ? info.screens.filter(s => s && s.id) : []
      if (info.blocked && info.blocked.reason) {
        endRun(slotKey); setSlot('')
        // `detail` comes from the model and is rendered as a React child, so an
        // object here would crash the route rather than show the blocked screen.
        const d = info.blocked.detail
        setBlocked({
          ...(BLOCKED[info.blocked.reason] || BLOCKED.other),
          detail: typeof d === 'string' ? d : typeof d === 'number' && Number.isFinite(d) ? String(d) : '',
        })
        setPhase('error'); return
      }
      if (!list.length) {
        endRun(slotKey); setSlot('')
        setErr(discoveryNote(info) ||
          'I got in, but there’s nothing in there I can render. Drop screenshots instead.')
        setPhase('error'); return
      }
      // Normalise before anything reads it: Array.isArray(flows) proves the
      // container is a list, not that its elements are usable, and a reply of
      // flows:[null] used to crash the picker on f.screenIds.
      const norm = normalizeScope({ ...info, screens: list }) || { ...info, screens: list, flows: [] }
      setScope(norm)
      const first = norm.flows[0]
      const preset = first && first.screenIds && first.screenIds.length
        ? first.screenIds.filter(id => list.some(s => s.id === id))
        : list.filter(s => s.canSee !== false).map(s => s.id)
      setPicked(preset)
      saveJob({ stage: 'scoping', slotKey, kind: det.kind, value: det.value, ts: Date.now(),
        scope: norm, picked: preset })
      setPhase('scoping')
    } catch (e) { setSlot(''); failWith(e, slotKey) }
  }

  // Step 2: critique only what was picked, in the picked order, reusing the same slot.
  const runScoped = async () => {
    if (!scope || !picked.length || !slot) return
    const byId = new Map(scope.screens.map(s => [s.id, s]))
    const picks = picked.map(id => byId.get(id)).filter(Boolean) as DiscoveryScreen[]
    startClock(); setWriting(false); setPhase('analyzing')
    activeSlotRef.current = slot
    try {
      saveJob({ stage: 'analyzing', slotKey: slot, screens: [], ts: Date.now() })
      await send(slot, SCOPED_PROMPT(picks, refBrief))
      const rep = await pollForReport<Report>(slot)
      finishReport(slot, [], rep)
      setSlot('')
    } catch (e) { const k = slot; setSlot(''); failWith(e, k) }
  }

  // Reap slots we created and never cleaned up. Runs before resume so the live job is spared.
  useEffect(() => {
    // Spare every persisted job AND every run still in flight. Sparing only the
    // newest job record deleted a background critique's slot the moment a second
    // one started.
    const keep = new Set<string>(loadLive())
    for (const j of loadJobs()) if (j.slotKey) keep.add(j.slotKey)
    const strays = loadSlots().filter(k => k && !keep.has(k))
    if (!strays.length) return
    saveSlots([...keep])
    for (const k of strays) designCritiqueApi.deleteSlot(k)
  }, [])

  // Resume whatever was in flight when the page went away.
  useEffect(() => {
    const all = loadJobs()
    const job = all[0]
    // Every OTHER persisted analyzing run gets a background poller so its result
    // is collected and written to history instead of being lost.
    for (const other of all.slice(1)) {
      if (other.stage === 'analyzing') resumeAnalyzing(other)
    }
    if (!job || !job.slotKey) return
    if (job.stage === 'analyzing') pollingRef.current.add(job.slotKey)

    if (job.stage === 'scoping' && job.scope) {
      setSlot(job.slotKey)
      // A job persisted by an earlier build can hold an un-normalised scope, so
      // repair on read rather than trusting what localStorage happens to contain.
      setScope(normalizeScope(job.scope) || job.scope)
      setPicked(Array.isArray(job.picked) ? job.picked : [])
      setRefBrief(job.refBrief || '')
      setPendingKind(job.kind || null)
      setCurrent({ report: null, screens: [] })
      setPhase('scoping')
      return
    }

    if (job.stage === 'scanning') {
      setSlot(job.slotKey); setPendingKind(job.kind || null)
      setCurrent({ report: null, screens: [] }); startClock(job.ts); setPhase('scanning')
      ;(async () => {
        try {
          const info = await pollForReport<DiscoveryInfo>(job.slotKey)
          const list = Array.isArray(info.screens) ? info.screens.filter(s => s && s.id) : []
          if (!list.length) {
            endRun(job.slotKey); setSlot('')
            setErr(discoveryNote(info) ||
              'I couldn’t find any screens I can render in there. Drop screenshots instead.')
            setPhase('error'); return
          }
          const sc: Scope = normalizeScope({ ...info, screens: list })
            || { ...info, screens: list, flows: [] }
          const first = sc.flows[0]
          const preset = first && first.screenIds && first.screenIds.length
            ? first.screenIds.filter(id => list.some(s => s.id === id))
            : list.filter(s => s.canSee !== false).map(s => s.id)
          setScope(sc); setPicked(preset)
          saveJob({ stage: 'scoping', slotKey: job.slotKey, kind: job.kind, value: job.value, ts: job.ts, scope: sc, picked: preset })
          setPhase('scoping')
        } catch (e) {
          const flag = e as Flagged
          if (flag && flag.cancelled) return
          if (flag && flag.timeout) {
            setErr('Still scanning. It’s kept running — come back to this page in a minute and it’ll pick up where it left off.')
            setPhase('error'); return
          }
          endRun(job.slotKey); setSlot('')
          setErr(e instanceof Error ? e.message : 'That scan didn’t finish.'); setPhase('error')
        }
      })()
      return
    }

    const uploaded = Array.isArray(job.screens) ? job.screens : []
    // Claim the slot: this branch shows the waiting screen for this run, so
    // isWatching() has to agree, or finishReport routes the result to the ready
    // chip and the foreground sits on "analyzing" for ever.
    activeSlotRef.current = job.slotKey
    setSlot(job.slotKey)
    setCurrent({ report: null, screens: uploaded }); startClock(job.ts); setWriting(false); setPhase('analyzing')
    ;(async () => {
      try { const rep = await pollForReport<Report>(job.slotKey); finishReport(job.slotKey, uploaded, rep) }
      catch (e) {
        pollingRef.current.delete(job.slotKey)
        const flag = e as Flagged
        if (flag && flag.cancelled) return
        if (flag && flag.timeout) {
          setErr('Still working on this one. It’s kept running — come back to this page in a minute and it’ll pick up where it left off.')
          setPhase('error'); return
        }
        endRun(job.slotKey)
        setErr(e instanceof Error ? e.message : 'That critique didn’t finish.'); setPhase('error')
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Follow the conversation: pin the thread to the newest turn as it arrives.
  useEffect(() => {
    const el = threadRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [asks, openAskId])

  // Keep the persisted pick in sync while you're deciding, so a reorder isn't lost.
  useEffect(() => {
    if (phase !== 'scoping' || !scope || !slot) return
    saveJob({ stage: 'scoping', slotKey: slot, kind: pendingKind, ts: Date.now(), scope, picked, refBrief })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, refBrief, phase])

  const busy = phase === 'uploading' || phase === 'analyzing' || phase === 'scanning'

  const report = current?.report ?? null
  const screens = (current && current.screens) || []
  const isFlow = screens.length > 1
  const shown = screens[Math.min(screenIdx, Math.max(0, screens.length - 1))] || null
  const thumbUrl = shown ? shown.url : ''

  // ── staging ────────────────────────────────────────────────────────────
  const addFiles = (fileList: FileList | File[] | null) => {
    const imgs = Array.from(fileList || []).filter(f => /^image\//.test(f.type || ''))
    if (!imgs.length) { setErr('Those weren’t image files.'); return }
    setErr('')
    setStaged(prev => {
      const room = MAX_SCREENS - prev.length
      if (room <= 0) { setErr('That’s the limit of ' + MAX_SCREENS + ' screens.'); return prev }
      if (imgs.length > room) setErr('Only added the first ' + room + ' — the limit is ' + MAX_SCREENS + ' screens.')
      return prev.concat(imgs.slice(0, room).map(f => ({ id: f.name + ':' + f.size + ':' + Math.random().toString(36).slice(2, 7), file: f, url: URL.createObjectURL(f) })))
    })
    if (phase === 'error') setPhase('new')
  }
  const dropStaged = (i: number) => setStaged(prev => {
    const next = prev.slice(); const [gone] = next.splice(i, 1)
    if (gone) { try { URL.revokeObjectURL(gone.url) } catch { /* ignore */ } }
    return next
  })
  const moveStaged = (i: number, dir: number) => setStaged(prev => {
    const j = i + dir; if (j < 0 || j >= prev.length) return prev
    const next = prev.slice(); const t = next[i]; next[i] = next[j]; next[j] = t; return next
  })
  /**
   * Attach a poller to a run that is persisted but unattended. Resume used to
   * start a poller for `loadJob()` only — the newest record — so a critique
   * backgrounded with `+ New` was never collected after a page revisit, and
   * clicking its pending row showed a spinner nothing was driving.
   */
  const resumeAnalyzing = (job: Job): void => {
    if (!job.slotKey || pollingRef.current.has(job.slotKey)) return
    pollingRef.current.add(job.slotKey)
    void (async () => {
      const uploaded = Array.isArray(job.screens) ? job.screens : []
      try {
        const rep = await pollForReport<Report>(job.slotKey)
        finishReport(job.slotKey, uploaded, rep)
      } catch (e) {
        failWith(e, job.slotKey)
      } finally {
        pollingRef.current.delete(job.slotKey)
      }
    })()
  }

  /**
   * The "nothing renderable in there" copy, read through normalizeScope so the
   * model's `cannotSee` / `note` cannot put an object into an error string. Both
   * empty-result branches used the raw payload before.
   */
  const discoveryNote = (info: Partial<Scope>): string => {
    const sc = normalizeScope(info)
    if (!sc) return ''
    return (sc.cannotSee && sc.cannotSee.length ? sc.cannotSee.join(' ') : '') || sc.note || ''
  }

  const clearStaged = () => setStaged(prev => { prev.forEach(s => { try { URL.revokeObjectURL(s.url) } catch { /* ignore */ } }); return [] })

  const onDrop = (e: React.DragEvent) => { e.preventDefault(); setDragging(false); if (!busy) addFiles(e.dataTransfer && e.dataTransfer.files) }
  const onDragOver = (e: React.DragEvent) => { e.preventDefault(); if (!dragging) setDragging(true) }
  const onDragLeave = (e: React.DragEvent) => { e.preventDefault(); setDragging(false) }
  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => { addFiles(e.target.files); e.target.value = '' }
  const pickFile = () => { if (inputRef.current) inputRef.current.click() }

  const canStart = !busy && (staged.length > 0 || !!refText.trim())
  const start = () => {
    if (!canStart) return
    if (staged.length) { const files = staged.map(s => s.file); clearStaged(); runImages(files) }
    else runRef(refText)
  }

  // ── highlight & ask ──────────────────────────────────────────────────────
  const captureSelection = () => {
    const w = window.getSelection && window.getSelection()
    if (!w || w.isCollapsed || !w.rangeCount) { setSel(null); return }
    const quote = String(w.toString()).replace(/\s+/g, ' ').trim()
    if (quote.length < 4 || quote.length > 320) { setSel(null); return }
    const r = w.getRangeAt(0).getBoundingClientRect()
    if (!r || (!r.width && !r.height)) { setSel(null); return }
    setSel({ quote, top: r.top, left: r.left + r.width / 2 })
  }

  const persistAsks = (list: Ask[]) => {
    const key = current && current.entryId
    if (!key) return
    const next = loadHistory().map(e => (e.id === key ? { ...e, asks: list } : e))
    saveHistory(next); setCritiques(next)
  }

  const runTurn = async (askId: string, question: string, seedWith: string | null) => {
    if (askPending) return
    setAskPending(true)
    const stamp = Date.now()
    setAsks(prev => prev.map(a => a.id === askId
      ? { ...a, turns: a.turns.concat([{ t: stamp, q: question || '', a: '', pending: true }]) }
      : a))
    try {
      if (!askSlotRef.current) {
        const k = await openSlot()
        askSlotRef.current = k
        await send(k, ASK_CONTEXT(report as Report, screens))
        await pollForText(k).catch(() => {})
      }
      await send(askSlotRef.current, seedWith
        ? ASK_PROMPT(seedWith, question)
        : 'Follow-up on the same highlighted text: ' + (question || 'Say more.') +
          '\n\nSame rules: 2-4 plain sentences, no bullets, no headings.')
      const text = await pollForText(askSlotRef.current)
      setAsks(prev => {
        const next = prev.map(a => a.id === askId
          ? { ...a, turns: a.turns.map(t => (t.t === stamp ? { ...t, a: text, pending: false } : t)) }
          : a)
        persistAsks(next); return next
      })
    } catch (e) {
      const flag = e as Flagged
      if (flag && flag.cancelled) return
      const msg = flag && flag.timeout ? 'That took too long — ask again.'
        : (e instanceof Error) ? e.message : 'Something went wrong.'
      setAsks(prev => prev.map(a => a.id === askId
        ? { ...a, turns: a.turns.map(t => (t.t === stamp ? { ...t, a: msg, pending: false, failed: true } : t)) }
        : a))
    } finally {
      // Every exit path, including the early cancelled return above, or the next
      // follow-up would be refused for the rest of the session.
      setAskPending(false)
    }
  }

  const askAbout = (quote: string, question: string) => {
    const id = 'a' + Date.now()
    setAsks(prev => prev.concat([{ id, quote, turns: [] }]))
    setOpenAskId(id); setSel(null); setAskDraft('')
    runTurn(id, question, quote)
  }
  const askFollowUp = (askId: string, question: string) => {
    if (!question || !question.trim()) return
    setAskDraft('')
    runTurn(askId, question.trim(), null)
  }
  const removeAsk = (id: string) => {
    const next = asks.filter(a => a.id !== id)
    setAsks(next); persistAsks(next)
    if (openAskId === id) setOpenAskId(null)
  }

  const sendScreenshots = () => {
    setPhase('new'); setCurrent(null); setScope(null); setPicked([]); setErr(''); setRefText('')
    setTimeout(() => { if (inputRef.current) inputRef.current.click() }, 0)
  }
  const critiqueRunning = () => {
    setPhase('new'); setCurrent(null); setScope(null); setPicked([]); setErr('')
    setRefText('http://localhost:')
  }

  const cancelRun = () => {
    if (askSlotRef.current) { dropSlot(askSlotRef.current); askSlotRef.current = '' }
    const k = activeSlotRef.current || slot
    // Cancel THIS run only, and clear only its job record: a bare clearJob()
    // removes every persisted run, which would discard a concurrent critique.
    if (k) { cancelledRef.current.add(k); endRun(k) }
    activeSlotRef.current = ''; setSlot(''); setScope(null); setPicked([]); setJustFinished(null)
    setPhase('new'); setCurrent(null); setErr(''); setWriting(false); setPendingKind(null)
    startedAtRef.current = 0; setElapsed(0)
    // Release this slot's flag once its poller has certainly observed it. Keyed
    // per slot so the timer cannot un-cancel a different run.
    if (k) setTimeout(() => { cancelledRef.current.delete(k) }, 2500)
  }

  const newCritique = () => {
    // 'scanning' is deliberately NOT here. A scan ends at a scope decision the
    // user has to make, so it cannot finish in the background: leaving it running
    // meant its completion wrote setScope/setPhase over a newer run, and the
    // pending row it earned could never resolve on its own. Treating it as
    // not-running routes it into the cleanup below, which ends it.
    const running = phase === 'analyzing' || phase === 'uploading'
    // Supersede anything mid-upload: it has no slot yet, so this counter is the
    // only way it can learn it no longer owns the screen.
    runSeqRef.current++
    clearStaged()
    if (running) {
      // The run is not cancelled, so give it a row in "Your critiques" with a
      // loading state. Without this the critique kept running invisibly and
      // looked like it had been thrown away.
      const k = activeSlotRef.current || slot
      if (k) setCritiques(beginPendingCritique(k, (current && current.screens) || []))
    }
    // Clear the job record too. Dropping only the slot left the run persisted, so
    // a reload resumed a critique the user had explicitly started over from.
    // Marking it cancelled makes an in-flight scan's poller exit on its next
    // check instead of grinding through eight failed polls against a dead slot.
    if (!running && slot) {
      cancelledRef.current.add(slot)
      setTimeout(() => { cancelledRef.current.delete(slot) }, 2500)
      endRun(slot)
    }
    if (!running) { setSlot(''); setScope(null); setPicked([]); setRefBrief('') }
    setPhase('new'); setCurrent(null); setMenuOpen(false); setErr(''); setBlocked(null); setRefText('')
    startedAtRef.current = 0; setElapsed(0); setWriting(false); setPendingKind(null)
  }
  const openExample = () => { setMenuOpen(false); showReport(SAMPLE_REPORT, SAMPLE_SCREENS) }
  /** Re-attach the view to a run that is still going. */
  const watchRun = (e: HistoryEntry) => {
    setMenuOpen(false)
    activeSlotRef.current = e.slotKey
    setSlot(e.slotKey)
    setCurrent({ report: null, screens: e.screens || [] })
    setJustFinished(null); setErr(''); setBlocked(null)
    const job = loadJobs().find(j => j.slotKey === e.slotKey)
    startClock(job && job.ts ? job.ts : e.ts)
    setPhase(e.screens && e.screens.length ? 'analyzing' : 'scanning')
    // Selecting a pending row only changed UI state before, so a run with no
    // poller sat at "analyzing" forever. Attach one if nothing is driving it.
    if (job && job.stage === 'analyzing') resumeAnalyzing(job)
    else if (!job) resumeAnalyzing({ stage: 'analyzing', slotKey: e.slotKey, screens: e.screens || [], ts: e.ts })
  }

  const selectCritique = (e: HistoryEntry) => {
    if (e.pending) { watchRun(e); return }
    if (!e.report) return
    setMenuOpen(false)
    const scr = e.screens && e.screens.length ? e.screens : (e.thumbUrl ? [{ step: 1, label: 'Screen 1', url: e.thumbUrl }] : [])
    showReport(e.report, scr, e)
  }
  const toggle = (i: number) => setOpen(prev => {
    const n = new Set(prev)
    if (n.has(i)) n.delete(i)
    else n.add(i)
    return n
  })
  const jumpTo = (i: number) => { setActive(i); setOpen(prev => new Set(prev).add(i)); const el = rowRefs.current[i]; if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }) }

  // scoping picker handlers
  const togglePick = (id: string) => setPicked(p => p.includes(id) ? p.filter(x => x !== id) : p.concat([id]))
  const dropPickAt = (id: string, overId: string) => setPicked(p => {
    const from = p.indexOf(id), to = p.indexOf(overId)
    if (from < 0 || to < 0 || from === to) return p
    const n = p.slice(); n.splice(from, 1); n.splice(to, 0, id); return n
  })
  const movePick = (id: string, dir: number) => setPicked(p => {
    const i = p.indexOf(id); const j = i + dir
    if (i < 0 || j < 0 || j >= p.length) return p
    const n = p.slice(); const t = n[i]; n[i] = n[j]; n[j] = t; return n
  })
  const useFlow = (f: Flow) => setPicked((f.screenIds || []).filter(id => scope ? scope.screens.some(s => s.id === id) : false))

  // ── derived finding structures ────────────────────────────────────────────
  const bySev = (a: Finding, b: Finding) => sevOf(a.severity).rank - sevOf(b.severity).rank
  const all: Finding[] = report && Array.isArray(report.findings) ? report.findings : []
  const stepOf = (f: Finding) => (Array.isArray(f.steps) && f.steps.length ? f.steps[0] : 1)
  const isFlowFinding = (f: Finding) => f.scope === 'flow' || (Array.isArray(f.steps) && f.steps.length > 1)
  const flowFindings = isFlow ? all.filter(isFlowFinding).sort(bySev) : []
  const screenFindings = all.filter(f => !isFlow || !isFlowFinding(f)).sort(bySev)

  const idxOf = new Map<Finding, number>()
  flowFindings.forEach((f, i) => idxOf.set(f, i))
  screenFindings.forEach((f, i) => idxOf.set(f, flowFindings.length + i))
  const pinNo = new Map<Finding, number>()
  screens.forEach((s) => {
    let n = 0
    screenFindings.filter(f => stepOf(f) === s.step).forEach(f => { n += 1; pinNo.set(f, n) })
  })
  if (!isFlow) screenFindings.forEach((f, i) => pinNo.set(f, i + 1))

  const stepRange = (f: Finding) => {
    // `steps` arrives from extractJson<Report>, which is an unchecked cast over
    // model output — a reply with "steps":"1" used to reach .sort() on a string and
    // take the whole report down. Trust the shape only when it really is an array.
    const st = Array.isArray(f.steps) ? f.steps.slice().sort((a, b) => a - b) : []
    if (!st.length) return 'flow'
    if (st.length === 1) return String(st[0])
    if (st.length === 2 && st[1] === st[0] + 1) return st[0] + '→' + st[1]
    return st[0] + '–' + st[st.length - 1]
  }

  const showStepOf = (f: Finding) => {
    if (!isFlow) return
    const i = screens.findIndex(s => s.step === stepOf(f))
    if (i >= 0) setScreenIdx(i)
  }

  // Pins for the screen currently on the canvas only. Location pins, never rectangles.
  const buildMarkers = (interactive: boolean): ReactNode[] => screenFindings
    .filter(f => !isFlow || (shown && stepOf(f) === shown.step))
    .map((f) => {
      const b = f.box; if (!b || typeof b.x !== 'number') return null
      const i = idxOf.get(f)!; const s = sevOf(f.severity); const on = active === i
      const cx = (b.x + (b.w || 0) / 2) * 100, cy = (b.y + (b.h || 0) / 2) * 100
      return (
        <span
          key={'mk' + i} title={pinNo.get(f) + '. ' + f.title}
          role={interactive ? 'button' : undefined}
          tabIndex={interactive ? 0 : undefined}
          aria-label={interactive ? pinNo.get(f) + '. ' + f.title : undefined}
          onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)}
          onClick={interactive ? (e) => { e.stopPropagation(); jumpTo(i) } : undefined}
          onKeyDown={interactive ? (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); jumpTo(i) }
          } : undefined}
          style={{ ...S.pinMarker, left: cx + '%', top: cy + '%', background: s.color, color: readableOn(s.color), cursor: interactive ? 'pointer' : 'default', opacity: on ? 1 : 0.92, transform: 'translate(-50%, -50%) scale(' + (on ? 1.25 : 1) + ')', boxShadow: on ? ('0 0 0 3px rgba(0,0,0,.5), 0 0 0 11px color-mix(in srgb, ' + s.color + ' 22%, transparent)') : '0 0 0 2px rgba(0,0,0,.5)', zIndex: on ? 3 : 2 }}
        >{String(pinNo.get(f))}</span>
      )
    }).filter(Boolean)

  // ── rail header ──
  const t = report ? (report.tally || {}) : {}
  let chips: ReactNode[] = (['catastrophe', 'major', 'minor', 'cosmetic'] as const).filter(k => (t[k] || 0) > 0).map(k => {
    const s = sevOf(k); const Icon = s.icon
    return <span key={k} style={{ ...S.chip, color: s.color }}><Icon size={12} />{(t[k] || 0) + ' ' + s.label.toLowerCase()}</span>
  })
  if (report && !chips.length) chips = [<span key="ok" style={{ ...S.chip, color: '#3fae6b' }}><Check size={12} />{i18nT('apps.designCritique.designCritiquePage.nothing_major')}</span>]

  const entryMeta = (e: HistoryEntry) => {
    const n = (e.screens && e.screens.length) || (e.thumbUrl ? 1 : 0)
    return (n > 1 ? n + ' screens · ' : n === 1 ? '1 screen · ' : '') + relTime(e.ts)
  }

  const railHead = (
    <div style={S.railHead}>
      <div style={S.title}><PencilRuler size={19} style={{ color: 'var(--accent)' }} />{i18nT('apps.designCritique.designCritiquePage.design_critique')}</div>
      <div style={S.railCtrls}>
        {phase !== 'new' ? <button style={S.railBtn} onClick={newCritique} title={i18nT('apps.designCritique.designCritiquePage.start_a_new_critique_anything_already_running_ke')}><Plus size={13} />{i18nT('apps.designCritique.designCritiquePage.new')}</button> : null}
        {busy ? <button style={S.runChip} onClick={() => { setJustFinished(null); setPhase(pendingKind && !screens.length ? 'scanning' : 'analyzing') }} title={i18nT('apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it')}><Spinner size={12} reduceMotion={reduceMotion} />{i18nT('apps.designCritique.designCritiquePage.running')}</button> : null}
        {(!busy && justFinished) ? <button style={{ ...S.runChip, ...S.readyChip }} onClick={() => { const h = loadHistory(); const mine = h.find(e => e.slotKey === justFinished.slotKey); if (mine) showReport(justFinished.report, justFinished.screens, mine); setJustFinished(null) }} title={justFinished.read}><Check size={12} />{i18nT('apps.designCritique.designCritiquePage.critique_ready')}</button> : null}
        {(phase !== 'new' && critiques.length) ? (
          <div style={{ position: 'relative' }}>
            <button style={S.railBtn} onClick={() => setMenuOpen(o => !o)} title={i18nT('apps.designCritique.designCritiquePage.switch_critique')}>{'History (' + critiques.length + ')'}<ChevronDown size={12} /></button>
            {menuOpen ? (
              <div style={S.menu}>
                {critiques.map(e => (
                  <Clickable key={e.id} style={S.menuItem} onClick={() => selectCritique(e)}
                    title={e.pending ? i18nT('apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it') : undefined} aria-busy={e.pending || undefined}>
                    {e.thumbUrl ? <img src={e.thumbUrl} style={S.menuThumb} alt="" /> : null}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ ...S.menuRead, ...(e.pending ? S.pendingRead : {}) }}>{e.pending ? i18nT('apps.designCritique.designCritiquePage.running') : (e.read || 'Critique')}</div>
                      <div style={S.menuTime}>{entryMeta(e)}</div>
                    </div>
                    {e.pending ? <Spinner size={12} reduceMotion={reduceMotion} /> : null}
                  </Clickable>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )

  // Splits a text run so previously-asked quotes render as clickable annotations.
  const withMarks = (text: string): ReactNode => {
    const str = String(text || '')
    if (!asks.length || !str) return str
    const low = str.toLowerCase()
    const hits = asks
      .map(a => ({ a, i: low.indexOf(a.quote.toLowerCase()) }))
      .filter(x => x.i >= 0)
      .sort((x, y) => x.i - y.i)
    if (!hits.length) return str
    const out: ReactNode[] = []
    let at = 0, n = 0
    for (const hit of hits) {
      if (hit.i < at) continue
      if (hit.i > at) out.push(str.slice(at, hit.i))
      out.push(
        <span
          key={'mk' + (n++)}
          style={S.mark}
          role="button"
          tabIndex={0}
          title={i18nT('apps.designCritique.designCritiquePage.your_question_click_to_see_the_answer')}
          onClick={(e) => { e.stopPropagation(); setOpenAskId(hit.a.id) }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              e.stopPropagation()
              setOpenAskId(hit.a.id)
            }
          }}
        >
          {str.substr(hit.i, hit.a.quote.length)}
        </span>
      )
      at = hit.i + hit.a.quote.length
    }
    if (at < str.length) out.push(str.slice(at))
    return out
  }

  const renderRow = (f: Finding) => {
    const i = idxOf.get(f)!
    const flowScoped = isFlow && isFlowFinding(f)
    return (
      <FindingRow
        key={i} finding={f} index={i} isOpen={open.has(i)} isActive={active === i}
        flowScoped={flowScoped} pinNum={pinNo.get(f)} stepRange={stepRange(f)}
        withMarks={withMarks}
        onEnter={() => setActive(i)} onLeave={() => setActive(null)}
        onClick={() => { toggle(i); if (!flowScoped) showStepOf(f) }}
        rowRef={(el) => { rowRefs.current[i] = el }}
      />
    )
  }

  // ── rail body ──
  let railBody: ReactNode
  if (phase === 'report' && report) {
    const tightenBlock: ReactNode[] = isFlow
      ? [
          flowFindings.length ? (
            <div key="flow">
              <div style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.across_the_flow')}<span style={{ fontWeight: 600, textTransform: 'none' }}>{'· ' + flowFindings.length}</span></div>
              {flowFindings.map(renderRow)}
            </div>
          ) : null,
          ...screens.map((sc) => {
            const mine = screenFindings.filter(f => stepOf(f) === sc.step)
            const onScreen = shown && shown.step === sc.step
            return (
              <div key={'st' + sc.step}>
                <Clickable style={S.stepH} onClick={() => setScreenIdx(screens.indexOf(sc))} title={i18nT('apps.designCritique.designCritiquePage.show_this_screen')}>
                  <span>{'Step ' + sc.step + ' · '}</span>
                  <span style={S.stepName} title={sc.label}>{shortLabel(sc.label)}</span>
                  {onScreen
                    ? <span style={{ ...S.stepCount, color: 'var(--accent)' }}>{i18nT('apps.designCritique.designCritiquePage.on_screen')}</span>
                    : <span style={{ ...S.stepCount, color: 'var(--muted)' }}>{mine.length ? mine.length + (mine.length === 1 ? ' finding' : ' findings') : 'nothing flagged'}</span>}
                </Clickable>
                {mine.map(renderRow)}
              </div>
            )
          }),
        ]
      : (screenFindings.length ? [<div key="wf"><div style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.what_i_d_tighten')}</div>{screenFindings.map(renderRow)}</div>] : [])

    railBody = (
      <>
        <p key="read" style={S.readLine}>{withMarks(report.overallRead || 'Design critique')}</p>
        {(report.health || isFlow) ? <div key="hh" style={S.health}>{[report.health, isFlow ? ' · ' + screens.length + ' screens' : ''].filter(Boolean).join('')}</div> : null}
        <div key="chips" style={S.chips}>{chips}</div>
        {(Array.isArray(report.keep) && report.keep.length) ? (
          <div key="keep">
            <div style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.what_s_working')}</div>
            {report.keep.map((k, i) => <div key={i} style={S.keepItem}><Check size={15} style={{ color: '#3fae6b', flexShrink: 0, marginTop: '2px' }} /><span>{k}</span></div>)}
          </div>
        ) : null}
        {tightenBlock}
        {(Array.isArray(report.couldNotSee) && report.couldNotSee.length) ? (
          <div key="cns">
            <div style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.couldn_t_see')}</div>
            {report.couldNotSee.map((c, i) => <div key={i} style={S.seeItem}>{'· ' + c}</div>)}
            <div style={{ display: 'flex', gap: '8px', marginTop: '10px', flexWrap: 'wrap' }}>
              <button style={S.linkBtn} onClick={sendScreenshots}><Upload size={13} />{i18nT('apps.designCritique.designCritiquePage.send_screenshots')}</button>
              <button style={S.linkBtn} onClick={critiqueRunning}><ImageIcon size={13} />{i18nT('apps.designCritique.designCritiquePage.point_me_at_a_running_url')}</button>
            </div>
          </div>
        ) : null}
      </>
    )
  } else if (busy) {
    railBody = (
      <>
        <p key="w" style={S.readLine}>{phase === 'uploading' ? 'Uploading…' : 'Reading your design…'}</p>
        <div key="sub" style={S.health}>{screens.length > 1
          ? 'A flow of ' + screens.length + ' screens — I’ll also check the jumps between them.'
          : screens.length ? 'One screen.'
          : pendingKind ? 'A ' + (KIND_LABEL[pendingKind] || 'design') + ' — rendering it before judging it.'
          : 'Getting the screens ready.'}</div>
        {screens.length ? (
          <div key="l">
            <div style={S.sectionH}>{screens.length > 1 ? 'Screens in order' : 'Screen'}</div>
            {screens.map((sc, i) => (
              <div key={i} style={{ ...S.seeItem, display: 'flex', gap: '8px' }}>
                <span style={{ color: 'var(--muted)', fontVariantNumeric: 'tabular-nums' }}>{(i + 1) + '.'}</span>
                <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sc.label || 'Screen ' + (i + 1)}</span>
              </div>
            ))}
          </div>
        ) : null}
        <div key="nx">
          <div style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.what_you_ll_get')}</div>
          {['An overall read of how it’s doing', 'What’s working, so it survives the next pass', 'The top things to tighten, with pins on the screen']
            .map((tx, i) => <div key={i} style={S.seeItem}>{'· ' + tx}</div>)}
        </div>
      </>
    )
  } else {
    railBody = (
      <>
        <p key="sub" style={S.sub}>{i18nT('apps.designCritique.designCritiquePage.point_me_at_a_screen_or_a_whole_flow_and_i_ll_re')}</p>
        <button key="ex" style={S.linkBtn} onClick={openExample}><Sparkle size={13} />{i18nT('apps.designCritique.designCritiquePage.see_an_example')}</button>
        <div key="h" style={S.sectionH}>{i18nT('apps.designCritique.designCritiquePage.your_critiques')}</div>
        {critiques.length
          ? <div key="list">{critiques.map(e => (
              <Clickable key={e.id} style={S.listItem} onClick={() => selectCritique(e)}
                title={e.pending ? i18nT('apps.designCritique.designCritiquePage.a_critique_is_still_running_click_to_watch_it') : undefined} aria-busy={e.pending || undefined}>
                {e.thumbUrl ? <img src={e.thumbUrl} style={S.listThumb} alt="" /> : null}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ ...S.listRead, ...(e.pending ? S.pendingRead : {}) }}>{e.pending ? i18nT('apps.designCritique.designCritiquePage.running') : (e.read || 'Critique')}</div>
                  <div style={S.listTime}>{entryMeta(e)}</div>
                </div>
                {e.pending
                  ? <Spinner size={13} reduceMotion={reduceMotion} />
                  : <ChevronRight size={15} style={{ color: 'var(--muted)', flexShrink: 0 }} />}
              </Clickable>
            ))}</div>
          : <div key="empty" style={S.emptyList}>{'No critiques yet.' + (narrow ? ' Drop a screenshot below.' : ' Drop a screenshot in the panel →')}</div>}
      </>
    )
  }

  const rail = (
    <div style={{ ...S.rail, ...(narrow ? S.railNarrow : {}) }} onMouseUp={phase === 'report' ? captureSelection : undefined}>
      {railHead}
      {railBody}
    </div>
  )

  // ── canvas ──
  let canvasInner: ReactNode
  if (phase === 'scoping' && scope) {
    canvasInner = (
      <ScopingPicker
        scope={scope} picked={picked} refBrief={refBrief} dragId={dragId}
        togglePick={togglePick} dropPickAt={dropPickAt} movePick={movePick} useFlow={useFlow}
        setDragId={setDragId} setRefBrief={setRefBrief} runScoped={runScoped} onStartOver={newCritique}
      />
    )
  } else if (phase === 'uploading' || phase === 'analyzing' || phase === 'scanning') {
    canvasInner = (
      <WaitingScreen
        phase={phase} elapsed={elapsed} writing={writing} reduceMotion={reduceMotion}
        screens={screens} pendingKind={pendingKind} onCancel={cancelRun}
      />
    )
  } else if (phase === 'report' && report && thumbUrl) {
    const markers = buildMarkers(true)
    const filmstrip = isFlow ? (
      <div style={S.strip}>
        {screens.map((sc, i) => {
          const mine = screenFindings.filter(f => stepOf(f) === sc.step)
          const worst = mine.slice().sort(bySev)[0]
          const on = i === screenIdx
          return (
            <button key={'th' + i} style={S.thumb} onClick={() => { setScreenIdx(i); setActive(null) }} title={'Step ' + sc.step + ' · ' + sc.label} aria-current={on ? 'true' : 'false'}>
              <div style={{ ...S.thumbBox, ...(on ? S.thumbBoxOn : {}) }}>
                <img src={sc.url} style={S.thumbImg} alt="" />
                {worst
                  ? <span style={{ ...S.thumbCount, color: sevOf(worst.severity).color }}><span style={{ width: '6px', height: '6px', borderRadius: '999px', background: sevOf(worst.severity).color, display: 'inline-block' }} />{String(mine.length)}</span>
                  : <span style={{ ...S.thumbCount, color: '#3fae6b' }}><Check size={11} /></span>}
              </div>
              <div style={{ ...S.thumbCap, ...(on ? { color: 'var(--text)' } : {}) }}>{sc.step + ' · ' + shortLabel(sc.label)}</div>
            </button>
          )
        })}
      </div>
    ) : null
    canvasInner = (
      <>
        {filmstrip}
        <div style={S.previewWrap}>
          <Clickable style={S.preview} onClick={() => setZoom(true)} title={i18nT('apps.designCritique.designCritiquePage.enlarge')}>
            <img src={thumbUrl} style={S.previewImg} alt={shown ? shown.label : 'the design'} />
            {markers}
          </Clickable>
          <div style={S.pvbar}>
            <Maximize2 size={12} />
            {'Click to enlarge' + (markers.length ? ' · numbered pins mark issues on this screen' : '') + (isFlow ? ' · pick a step above' : '')}
          </div>
        </div>
      </>
    )
  } else if (phase === 'report' && report) {
    canvasInner = (
      <div style={S.previewWrap}>
        <div style={{ ...S.muted, maxWidth: '420px', textAlign: 'center' }}>{i18nT('apps.designCritique.designCritiquePage.no_screens_to_show_the_critic_couldn_t_get_pixel')}</div>
      </div>
    )
  } else {
    canvasInner = (
      <Composer
        staged={staged} refText={refText} dragging={dragging} blocked={blocked} showAuth={showAuth}
        busy={busy} err={err} inputRef={inputRef}
        onPick={onPick} onDrop={onDrop} onDragOver={onDragOver} onDragLeave={onDragLeave}
        pickFile={pickFile} dropStaged={dropStaged} moveStaged={moveStaged} clearStaged={clearStaged}
        start={start} setRefText={setRefText} setBlocked={setBlocked} setShowAuth={setShowAuth}
        onTryAgain={() => runRef(refText)}
      />
    )
  }
  const canvas = <div style={S.canvas}>{canvasInner}</div>

  const lightbox = (zoom && thumbUrl) ? (
    <div
      style={S.lbOverlay}
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) setZoom(false) }}
    >
      <div style={S.lbInner} role="dialog" aria-modal="true" aria-label={i18nT('apps.designCritique.designCritiquePage.full_size_view')}>
        <img src={thumbUrl} style={S.lbImg} alt={i18nT('apps.designCritique.designCritiquePage.full_size')} />
        {buildMarkers(false)}
      </div>
      <button
        style={S.lbClose}
        onClick={() => setZoom(false)}
        title={i18nT('apps.designCritique.designCritiquePage.close_esc')}
        aria-label={i18nT('apps.designCritique.askLayer.close')}
      ><X size={18} /></button>
    </div>
  ) : null

  return (
    <div ref={rootRef} style={{ ...S.shell, ...(narrow ? { flexDirection: 'column' } : {}) }}>
      {rail}
      {canvas}
      {lightbox}
      {phase === 'report' ? (
        <AskLayer
          sel={sel} asks={asks} openAskId={openAskId} askDraft={askDraft} reduceMotion={reduceMotion}
          threadRef={threadRef} setOpenAskId={setOpenAskId} setSel={setSel} setAskDraft={setAskDraft}
          askAbout={askAbout} askFollowUp={askFollowUp} removeAsk={removeAsk} pending={askPending}
        />
      ) : null}
      {toasts.length ? (
        <div style={S.toastWrap}>
          {toasts.map(t2 => <div key={t2.id} style={{ ...S.toast, ...(t2.type === 'error' ? { borderColor: 'var(--error, #e5484d)', color: 'var(--error, #e5484d)' } : {}) }}>{t2.msg}</div>)}
        </div>
      ) : null}
    </div>
  )
}
