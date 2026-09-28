/**
 * fuzzyMatch — a small, dependency-free scored fuzzy matcher for the
 * Search Everywhere command palette.
 *
 * Given a `query` and a `candidate`, it returns a {@link FuzzyMatchResult}
 * `{ score, indices }` where `indices` are the positions in `candidate` that
 * matched query characters — providers use these to render highlight marks
 * (as React nodes, never HTML strings; see the `frontend-security` lint rule).
 *
 * The result shape mirrors Codex's `codex_file_search` output (a numeric score
 * plus highlight indices) so palette providers can sort and highlight uniformly.
 *
 * Contract:
 *  - Non-empty query that is NOT a subsequence of candidate → `null`
 *    (providers filter these out).
 *  - Empty/whitespace-only query → neutral `{ score: 0, indices: [] }`
 *    so providers can fall back to showing recents.
 *  - Otherwise → `{ score > 0, indices }`, with exact matches scoring highest.
 *
 * Matching is case-insensitive; scoring rewards first-character matches,
 * word-boundary and camelCase boundaries, and consecutive runs, while
 * penalizing leading and interior gaps.
 */

export interface FuzzyMatchResult {
  /** Higher is a better match. Exact (case-insensitive) equality scores highest. */
  score: number;
  /** Indices into `candidate` of matched characters, ascending. For highlighting. */
  indices: number[];
}

// Scoring weights. Tuned so exact > prefix > word-boundary subsequence > scattered.
const SCORE_PER_MATCH = 16;
const BONUS_FIRST_CHAR = 12; // matched candidate index 0
const BONUS_BOUNDARY = 8; // char after a separator (space - _ / . : etc.)
const BONUS_CAMEL = 7; // lower→Upper transition (camelCase word start)
const BONUS_CONSECUTIVE = 8; // directly follows the previously matched char
const PENALTY_LEADING_GAP = 3; // per char before the first match (capped)
const MAX_LEADING_PENALTY = 9;
const PENALTY_GAP = 1; // per skipped char between two matches (capped)
const MAX_GAP_PENALTY = 5;
const BONUS_EXACT = 100; // candidate equals query (case-insensitive)
const BONUS_PREFIX = 20; // candidate starts with query (case-insensitive)
const COVERAGE_WEIGHT = 20; // * (matchedLen / candidateLen)

const SEPARATORS = new Set([' ', '-', '_', '/', '.', ':', '\\', '|', '\t']);

function isUpper(ch: string): boolean {
  return ch >= 'A' && ch <= 'Z';
}

function isLower(ch: string): boolean {
  return ch >= 'a' && ch <= 'z';
}

/**
 * Score a single fuzzy match between `query` and `candidate`.
 *
 * @returns A {@link FuzzyMatchResult} on match (or empty query), or `null` when
 *          a non-empty query is not a subsequence of `candidate`.
 */
export function fuzzyMatch(query: string, candidate: string): FuzzyMatchResult | null {
  const q = query.trim();

  // Empty query → neutral result; providers use this to show recents.
  if (q.length === 0) {
    return { score: 0, indices: [] };
  }

  if (candidate.length === 0) {
    return null;
  }

  const qLower = q.toLowerCase();
  const cLower = candidate.toLowerCase();

  // Greedy left-to-right subsequence scan over the candidate.
  const indices: number[] = [];
  let qi = 0;
  let score = 0;
  let prevMatchIdx = -1;

  for (let ci = 0; ci < candidate.length && qi < qLower.length; ci++) {
    if (cLower[ci] !== qLower[qi]) {
      continue;
    }

    // This candidate char matches the current query char.
    let charScore = SCORE_PER_MATCH;

    const prevChar = ci > 0 ? candidate[ci - 1] : '';
    const curChar = candidate[ci];

    if (ci === 0) {
      charScore += BONUS_FIRST_CHAR;
    } else if (SEPARATORS.has(prevChar)) {
      charScore += BONUS_BOUNDARY;
    } else if (isLower(prevChar) && isUpper(curChar)) {
      charScore += BONUS_CAMEL;
    }

    if (prevMatchIdx >= 0) {
      if (ci === prevMatchIdx + 1) {
        charScore += BONUS_CONSECUTIVE;
      } else {
        const gap = ci - prevMatchIdx - 1;
        charScore -= Math.min(gap * PENALTY_GAP, MAX_GAP_PENALTY);
      }
    } else {
      // Leading gap before the very first matched char.
      charScore -= Math.min(ci * PENALTY_LEADING_GAP, MAX_LEADING_PENALTY);
    }

    score += charScore;
    indices.push(ci);
    prevMatchIdx = ci;
    qi++;
  }

  // Not all query chars were consumed → not a subsequence → no match.
  if (qi < qLower.length) {
    return null;
  }

  // Whole-match bonuses.
  if (cLower === qLower) {
    score += BONUS_EXACT;
  } else if (cLower.startsWith(qLower)) {
    score += BONUS_PREFIX;
  }

  // Reward tighter candidates (a full short match beats a sparse long one).
  score += Math.round((COVERAGE_WEIGHT * qLower.length) / candidate.length);

  // Guarantee a positive score for any genuine match.
  if (score < 1) {
    score = 1;
  }

  return { score, indices };
}

/** Minimal shape consumed by {@link compareByScoreThenName}. */
export interface ScoredNamed {
  score: number;
  name: string;
}

/**
 * Sort comparator: score descending, then name ascending (case-insensitive,
 * locale-aware). Stable tie-break keeps palette ordering deterministic.
 *
 * Usage: `results.sort(compareByScoreThenName)`.
 */
export function compareByScoreThenName(a: ScoredNamed, b: ScoredNamed): number {
  if (b.score !== a.score) {
    return b.score - a.score;
  }
  return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
}

/**
 * Comparator factory for arbitrary item shapes — supply accessors for the
 * score and the display name. Same ordering as {@link compareByScoreThenName}.
 */
export function makeScoreThenNameComparator<T>(
  getScore: (item: T) => number,
  getName: (item: T) => string,
): (a: T, b: T) => number {
  return (a, b) => {
    const sa = getScore(a);
    const sb = getScore(b);
    if (sb !== sa) {
      return sb - sa;
    }
    return getName(a).localeCompare(getName(b), undefined, { sensitivity: 'base' });
  };
}

/**
 * Contiguous indices of the (case-insensitive) *query* within *text*, for
 * highlighting an exact matched term (e.g. inside a content-search snippet).
 * Empty when the query is blank or absent from the text. Complements
 * {@link fuzzyMatch}, which scores scattered subsequence matches — snippet
 * highlighting wants the literal substring only.
 */
export function substringIndices(query: string, text: string): number[] {
  const q = query.trim()
  if (!q) return []
  const i = text.toLowerCase().indexOf(q.toLowerCase())
  if (i < 0) return []
  return Array.from({ length: q.length }, (_, k) => i + k)
}
