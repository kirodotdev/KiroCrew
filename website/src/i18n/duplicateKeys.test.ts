/**
 * Duplicate-key guard for the locale catalogs.
 *
 * WHY THIS EXISTS. `ja.json` carried ELEVEN keys that appeared twice inside the
 * same object (`how_to_fix`, `copied`, `remedy_*`, …). Both `JSON.parse` and
 * Python's `json` keep the LAST occurrence, so the earlier value was dead
 * weight no reader ever saw — and, far worse, the file could not be safely
 * round-tripped: any tool that reserialised it silently DROPPED the shadowed
 * translations.
 *
 * That is not hypothetical. The eleven duplicates were finally removed as an
 * incidental side effect of an unrelated feature PR whose tooling happened to
 * reserialise the file. Eleven Japanese strings changed value inside a diff
 * nobody was reviewing for translation content. This test is the guard that was
 * missing: it makes the trap fail loudly when a duplicate is introduced,
 * instead of years later inside someone else's diff.
 *
 * WHY IT PARSES RAW TEXT. A test built on `JSON.parse` — including one using a
 * reviver, which runs only AFTER the collapse — passes on a file with duplicate
 * keys and is therefore worthless for this property. The only way to see a
 * duplicate is to look at the bytes, so this walks the source with a minimal
 * JSON scanner that tracks the key set of each object it enters.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

const LOCALES_DIR = join(__dirname, 'locales')

/** Every catalog on disk, so a newly added language is covered with no edit here. */
const CATALOG_FILES = readdirSync(LOCALES_DIR)
  .filter(name => name.endsWith('.json'))
  .sort()

interface Duplicate {
  /** Dotted path of the containing object ('' for the document root). */
  container: string
  key: string
  /** How many times the key appears in that one object. */
  count: number
}

/**
 * Scan JSON source for keys repeated within a single object.
 *
 * A deliberately small hand-rolled scanner rather than a dependency: it only
 * has to tell object keys from everything else, which needs just string
 * handling (with escapes) and brace/bracket depth. Values are never
 * interpreted, so there is no number/keyword parsing to get wrong.
 *
 * Counts are harvested when each object CLOSES, which is what keeps the report
 * scoped per object — the same key legitimately appears in many sections, and a
 * whole-file count would flag every one of them.
 */
function scan(source: string): Duplicate[] {
  const duplicates: Duplicate[] = []
  // One frame per open container. Arrays push `keys: null` so an array's string
  // ELEMENTS are never mistaken for keys.
  const stack: { keys: Map<string, number> | null; path: string }[] = []
  // The most recent string literal. It only becomes a key once a ':' follows it
  // at object level, and it is still needed after that to name a nested
  // container — so it is cleared on ',' and on container boundaries, not on ':'.
  let lastString: string | null = null
  let i = 0

  const readString = (): string => {
    // Caller positions us ON the opening quote.
    let raw = ''
    i += 1
    while (i < source.length) {
      const ch = source[i]
      if (ch === '\\') {
        // Collect the escape verbatim for now; it is DECODED below.
        raw += source[i] + (source[i + 1] ?? '')
        i += 2
        continue
      }
      if (ch === '"') {
        i += 1
        break
      }
      raw += ch
      i += 1
    }
    // Compare keys by their DECODED value, because that is what a JSON reader
    // sees. `"n\u0061me"` and `"name"` are the same key per ECMA-404, so
    // `{"name": 1, "n\u0061me": 2}` IS a duplicate that JSON.parse collapses --
    // and a scanner comparing the raw bytes would call them distinct and wave
    // the real defect through. Escapes are rare in these catalogs but free to
    // handle, and the whole point of this guard is that it does not miss one.
    //
    // `raw` is by construction the body of a JSON string, so re-quoting it and
    // parsing decodes every escape (including surrogate pairs) with no
    // hand-rolled table. A malformed escape cannot parse; fall back to the raw
    // text so the scan still completes rather than throwing on a broken file --
    // the JSON parse elsewhere in the suite is what reports that.
    try {
      return JSON.parse(`"${raw}"`) as string
    } catch {
      return raw
    }
  }

  while (i < source.length) {
    const ch = source[i]

    if (ch === '"') {
      lastString = readString()
      continue
    }

    if (ch === '{' || ch === '[') {
      const parentPath = stack.length ? stack[stack.length - 1].path : ''
      const path =
        lastString !== null
          ? parentPath
            ? `${parentPath}.${lastString}`
            : lastString
          : parentPath
      stack.push({ keys: ch === '{' ? new Map() : null, path })
      lastString = null
      i += 1
      continue
    }

    if (ch === '}' || ch === ']') {
      const frame = stack.pop()
      if (frame?.keys) {
        for (const [key, count] of frame.keys) {
          if (count > 1) duplicates.push({ container: frame.path, key, count })
        }
      }
      lastString = null
      i += 1
      continue
    }

    if (ch === ':') {
      const frame = stack[stack.length - 1]
      if (frame?.keys && lastString !== null) {
        frame.keys.set(lastString, (frame.keys.get(lastString) ?? 0) + 1)
      }
      i += 1
      continue
    }

    if (ch === ',') {
      lastString = null
      i += 1
      continue
    }

    i += 1
  }

  return duplicates
}

describe('locale catalogs have no duplicate keys', () => {
  it('found catalogs to check', () => {
    // A floor, not an exact count: the point is to catch a directory read that
    // silently matched nothing, which would make every case below vacuous.
    expect(CATALOG_FILES.length).toBeGreaterThan(5)
  })

  it.each(CATALOG_FILES)('%s defines each key at most once per object', name => {
    const source = readFileSync(join(LOCALES_DIR, name), 'utf8')
    expect(
      scan(source).map(
        d => `${d.container ? `${d.container}.` : ''}${d.key} (×${d.count})`,
      ),
    ).toEqual([])
  })
})

describe('the duplicate scanner itself', () => {
  // The scanner IS the test. A scanner that silently reported nothing would
  // make every catalog above pass vacuously — the exact failure mode of a
  // JSON.parse-based check — so its detection is asserted directly.
  it('detects a repeat inside one object', () => {
    expect(scan('{"a": "1", "a": "2"}')).toEqual([{ container: '', key: 'a', count: 2 }])
  })

  it('reports the containing path for a nested repeat', () => {
    expect(scan('{"outer": {"dup": 1, "dup": 2}}')).toEqual([
      { container: 'outer', key: 'dup', count: 2 },
    ])
  })

  it('does not flag the same key in two DIFFERENT objects', () => {
    // The legitimate case a naive whole-file key count would fail on: every
    // catalog reuses names like "cancel" across many sections.
    expect(scan('{"a": {"cancel": 1}, "b": {"cancel": 2}}')).toEqual([])
  })

  it('does not treat array string elements as keys', () => {
    expect(scan('{"list": ["x", "x", "x"]}')).toEqual([])
  })

  it('does not treat a colon inside a string value as a key separator', () => {
    expect(scan('{"note": "time: 10:30", "other": "a: b"}')).toEqual([])
  })

  it('does not treat an escaped quote as the end of a string', () => {
    expect(scan('{"quote": "she said \\"hi\\"", "quote2": "ok"}')).toEqual([])
  })

  it('does not treat a brace inside a string value as a container', () => {
    // Interpolation placeholders put braces inside values throughout these
    // catalogs, so a scanner reacting to them would mis-scope every key.
    expect(scan('{"greet": "hello {{name}}", "greet": "dup"}')).toEqual([
      { container: '', key: 'greet', count: 2 },
    ])
  })

  it('treats an escaped-equivalent key as the SAME key', () => {
    // `\u0061` is `a`, so both keys decode to "name" and JSON.parse keeps only
    // the second -- a real duplicate that silently discards "first". Comparing
    // raw bytes would call them distinct and miss exactly the defect this guard
    // exists to catch.
    expect(scan('{"name": "first", "n\\u0061me": "second"}')).toEqual([
      { container: '', key: 'name', count: 2 },
    ])
  })

  it('decodes escapes when reporting a container path', () => {
    expect(scan('{"se\\u0063tion": {"dup": 1, "dup": 2}}')).toEqual([
      { container: 'section', key: 'dup', count: 2 },
    ])
  })

  it('still distinguishes keys that decode differently', () => {
    // The inverse guard: decoding must not over-collapse. `\u0062` is `b`, so
    // these are genuinely two different keys.
    expect(scan('{"a": 1, "\\u0062": 2}')).toEqual([])
  })

  it('does not throw on a malformed escape', () => {
    // A broken catalog is reported by the suite's JSON parse, not by this
    // scanner crashing mid-walk.
    expect(() => scan('{"bad\\q": 1, "ok": 2}')).not.toThrow()
  })

  it('catches the historical ja.json shape', () => {
    // The real regression, reduced: two sibling sections each carrying a copy of
    // one key (legitimate), plus one genuine in-object repeat (the defect).
    const source = `{
      "components": {
        "gate": { "how_to_fix": "A", "other": 1, "how_to_fix": "B" },
        "panel": { "how_to_fix": "C" }
      }
    }`
    expect(scan(source)).toEqual([
      { container: 'components.gate', key: 'how_to_fix', count: 2 },
    ])
  })
})
