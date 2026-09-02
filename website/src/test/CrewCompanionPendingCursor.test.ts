/**
 * The pending-fire cursor must survive a reload, or history replays.
 *
 * Reading `/pending` is non-destructive by design, so the backend keeps every fire
 * and answers "everything after `since`". That is what lets a lost response or a
 * second display's overlay still see a reminder. It also means the queue outlives
 * the page — and starting from 0 on every load re-delivered the whole history, so
 * each restart of the desktop shell put an already-seen, already-fired reminder back
 * on screen. From the user's side it read as a bubble that could not be closed.
 *
 * The desktop app this was ported from never hit this: its queue lived in the
 * Electron main process and died with the app.
 */
import { describe, it, expect, beforeEach } from 'vitest'

const CURSOR_KEY = 'cc:pendingCursor'

/**
 * The two helpers as pet.tsx defines them. Duplicated rather than exported: the
 * module pulls in the whole overlay (bridge, rAF loops, CSS) on import, and the
 * behaviour under test is this pair of rules, not the wiring.
 */
function readStoredCursor(): number {
  try {
    const n = Number(window.localStorage.getItem(CURSOR_KEY))
    return Number.isFinite(n) && n > 0 ? n : 0
  } catch {
    return 0
  }
}

function writeStoredCursor(n: number): void {
  try {
    window.localStorage.setItem(CURSOR_KEY, String(n))
  } catch {
    /* ignored */
  }
}

/**
 * The drain rule from pet.tsx's poll, in isolation.
 *
 * `serverSeq` is the backend's in-memory counter — it starts again at 0 whenever
 * the gateway restarts, which is the whole reason this rule exists.
 */
function drain(
  serverSeq: number,
  pending: { seq: number }[],
): { asked: number[]; shown: number[]; stored: number } {
  const asked: number[] = []
  const call = (since: number) => {
    asked.push(since)
    return { cursor: serverSeq, fires: pending.filter((f) => f.seq > since) }
  }

  const since = readStoredCursor()
  let data = call(since)
  // A cursor below the one we asked from means the sequence restarted.
  if (data.cursor < since) data = call(0)
  writeStoredCursor(data.cursor)
  return { asked, shown: data.fires.map((f) => f.seq), stored: readStoredCursor() }
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('a restarted gateway must not swallow a fire', () => {
  it('re-reads from zero when the server cursor went backwards', () => {
    // Yesterday's session left the cursor at 42; the gateway has since restarted
    // and a newly due reminder is sitting there as seq 1.
    writeStoredCursor(42)
    const r = drain(1, [{ seq: 1 }])

    expect(r.asked).toEqual([42, 0])
    expect(r.shown).toEqual([1])   // without the re-read this is [] — and gone for good
    expect(r.stored).toBe(1)
  })

  it('does not re-read when the cursor is merely unchanged', () => {
    // The ordinary quiet poll: nothing new, and no reason to replay anything.
    writeStoredCursor(7)
    const r = drain(7, [{ seq: 7 }])

    expect(r.asked).toEqual([7])
    expect(r.shown).toEqual([])
    expect(r.stored).toBe(7)
  })

  it('still shows only what is new when the server moved forward', () => {
    writeStoredCursor(7)
    const r = drain(9, [{ seq: 7 }, { seq: 8 }, { seq: 9 }])

    expect(r.asked).toEqual([7])
    expect(r.shown).toEqual([8, 9])
    expect(r.stored).toBe(9)
  })
})

describe('a batch of fires loses none of them', () => {
  /** The drain rule: show the oldest speakable, and only move the cursor to it. */
  function drainOnce(
    serverSeq: number,
    pending: { seq: number; kind: string }[],
  ): { shown: number | null; stored: number } {
    const since = readStoredCursor()
    const fires = pending.filter((f) => f.seq > since)
    const speakable = fires.filter((f) => f.kind !== 'command')
    if (speakable.length === 0) {
      writeStoredCursor(serverSeq)
      return { shown: null, stored: readStoredCursor() }
    }
    const shown = speakable[0]
    writeStoredCursor(shown.seq)
    return { shown: shown.seq, stored: readStoredCursor() }
  }

  it('shows two same-tick reminders across two polls instead of dropping one', () => {
    // Both came due in the same second, so /pending returns them together.
    const pending = [
      { seq: 1, kind: 'reminder' },
      { seq: 2, kind: 'reminder' },
    ]
    const first = drainOnce(2, pending)
    expect(first.shown).toBe(1)      // the OLDEST, not the newest
    expect(first.stored).toBe(1)     // cursor did NOT jump past seq 2

    const second = drainOnce(2, pending)
    expect(second.shown).toBe(2)     // the other one still arrives
    expect(second.stored).toBe(2)
  })

  it('consumes a command-only batch whole, since commands are acted on not shown', () => {
    const pending = [{ seq: 1, kind: 'command' }, { seq: 2, kind: 'command' }]
    const r = drainOnce(2, pending)
    expect(r.shown).toBeNull()
    expect(r.stored).toBe(2)
  })

  it('does not let a trailing command strand an earlier reminder', () => {
    const pending = [
      { seq: 1, kind: 'reminder' },
      { seq: 2, kind: 'command' },
    ]
    const r = drainOnce(2, pending)
    expect(r.shown).toBe(1)
    expect(r.stored).toBe(1)
  })
})

describe('pending cursor persistence', () => {
  it('starts at 0 the very first time, so nothing is missed', () => {
    expect(readStoredCursor()).toBe(0)
  })

  it('resumes where the last run left off instead of replaying', () => {
    writeStoredCursor(7)
    expect(readStoredCursor()).toBe(7)
  })

  it('a fire newer than the cursor is still delivered late', () => {
    // The property that forbids the tempting "just start at the current cursor"
    // shortcut: a reminder that fired while the companion was off must arrive on
    // the user's return, which only works if the cursor is BEHIND that fire.
    writeStoredCursor(7)
    const fires = [{ seq: 7 }, { seq: 8 }].filter((f) => f.seq > readStoredCursor())
    expect(fires.map((f) => f.seq)).toEqual([8])
  })

  it('falls back to 0 on a corrupt value rather than muting everything', () => {
    // Failing towards "replay once" is recoverable; failing towards a huge cursor
    // would silently swallow every future reminder.
    window.localStorage.setItem(CURSOR_KEY, 'not-a-number')
    expect(readStoredCursor()).toBe(0)
    window.localStorage.setItem(CURSOR_KEY, '-5')
    expect(readStoredCursor()).toBe(0)
  })

  it('survives a simulated restart: the same fire is not shown twice', () => {
    const history = [{ seq: 1 }, { seq: 2 }]
    // First run drains everything and records the cursor.
    const firstRun = history.filter((f) => f.seq > readStoredCursor())
    expect(firstRun).toHaveLength(2)
    writeStoredCursor(2)
    // Restart: same backend history, nothing new to show.
    const secondRun = history.filter((f) => f.seq > readStoredCursor())
    expect(secondRun).toHaveLength(0)
  })
})

/**
 * The third cursor bug and its rule: the cursor commits ONLY when a fire's fate
 * is decided — shown, or deliberately consumed. A fire deferred by a sticky hold
 * leaves the cursor untouched so the next poll re-delivers it; nextBubble keeps
 * no queue of its own (show:null drops the incoming), so the unmoved cursor IS
 * the retry mechanism. This models pet.tsx's drain in isolation, like drain()
 * above, because importing the overlay pulls in the bridge and rAF loops.
 */
describe('cursor commits only on admission (sticky-hold deferral)', () => {
  const STICKY_HOLD_MS = 12_000

  interface Slot { sticky: boolean; at: number }

  /** pet.tsx's ambient drain rule, isolated: returns the cursor after one poll. */
  function drainOnce(
    cursor: number,
    fires: { seq: number; kind: string }[],
    slot: Slot | null,
    now: number,
  ): { cursor: number; shown: number[] } {
    const shown: number[] = []
    const speakable = fires.filter((f) => f.seq > cursor && f.kind !== 'command')
    if (speakable.length === 0) return { cursor, shown }
    const latest = speakable[0]
    const ambient = latest.kind === 'reminder' || latest.kind === 'break'
    if (ambient && slot?.sticky && now - slot.at < STICKY_HOLD_MS) {
      // Deferred: no commit, no display.
      return { cursor, shown }
    }
    // Admitted: the single commit point.
    shown.push(latest.seq)
    return { cursor: latest.seq, shown }
  }

  it('a reminder during a sticky hold does not advance the cursor and is shown after the hold', () => {
    writeStoredCursor(0)
    const fires = [{ seq: 7, kind: 'reminder' }]
    const hold: Slot = { sticky: true, at: 100_000 }

    // Poll while the approval bubble holds the slot: nothing shown, cursor pinned.
    const during = drainOnce(readStoredCursor(), fires, hold, 100_000 + 2_000)
    expect(during.shown).toHaveLength(0)
    expect(during.cursor).toBe(0)
    writeStoredCursor(during.cursor)

    // Poll after the hold lapses: the SAME fire is re-delivered and shown.
    const after = drainOnce(readStoredCursor(), fires, hold, 100_000 + STICKY_HOLD_MS + 1)
    expect(after.shown).toEqual([7])
    expect(after.cursor).toBe(7)
  })

  it('a command fire is consumed regardless of any hold', () => {
    // Commands are acted on, never drawn — they are filtered out of speakable and
    // consumed by the whole-batch path; a hold must not make a window open twice.
    const fires = [{ seq: 3, kind: 'command' }]
    const hold: Slot = { sticky: true, at: 0 }
    const r = drainOnce(0, fires, hold, 1_000)
    // No speakable fires: cursor moves via the batch path in pet.tsx (modeled
    // here as no-show, no-pin — the command was already executed).
    expect(r.shown).toHaveLength(0)
  })

  it('oldest-first batch order is preserved by admission-time commit', () => {
    writeStoredCursor(0)
    const fires = [
      { seq: 4, kind: 'reminder' },
      { seq: 5, kind: 'reminder' },
    ]
    // No hold: first poll shows the OLDEST and commits only to it.
    const first = drainOnce(readStoredCursor(), fires, null, 1_000)
    expect(first.shown).toEqual([4])
    writeStoredCursor(first.cursor)
    // Next poll delivers the second.
    const second = drainOnce(readStoredCursor(), fires, null, 3_000)
    expect(second.shown).toEqual([5])
  })
})
