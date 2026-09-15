// Grill question tree — pure state model (flat node list) + reducer + selectors.
// Backend contract mirrors handlers._handle_grill_expand.

export type GrillKind = "root" | "clarifier" | "research"
export type GrillStatus = "open" | "answered" | "promoted" | "pruned"

export interface GrillNode {
  id: string
  parent: string | null
  kind: GrillKind
  text: string
  recommended: string
  answer: string
  origin: "grill" | "emergent" | ""
  status: GrillStatus
}

export type GrillAction =
  | { type: "addChildren"; nodes: GrillNode[] }
  | { type: "setAnswer"; id: string; answer: string }
  | { type: "accept"; id: string }
  | { type: "investigateInstead"; id: string }
  | { type: "togglePromote"; id: string }
  | { type: "prune"; id: string }
  | { type: "edit"; id: string; text: string }

const map = (t: GrillNode[], id: string, fn: (n: GrillNode) => GrillNode): GrillNode[] =>
  t.map(n => (n.id === id ? fn(n) : n))

/** ids of node + all its descendants (for prune). */
function subtreeIds(tree: GrillNode[], id: string): Set<string> {
  const out = new Set([id])
  let grew = true
  while (grew) {
    grew = false
    for (const n of tree) {
      if (n.parent && out.has(n.parent) && !out.has(n.id)) {
        out.add(n.id)
        grew = true
      }
    }
  }
  return out
}

export function grillReducer(tree: GrillNode[], a: GrillAction): GrillNode[] {
  switch (a.type) {
    case "addChildren":
      // New research nodes default to promoted (included) so the fast path works;
      // clarifiers stay open until answered.
      return tree.concat(
        a.nodes.map(n => (n.kind === "research" ? { ...n, status: "promoted" } : n))
      )
    case "setAnswer":
      return map(tree, a.id, n => ({ ...n, answer: a.answer, status: "answered" }))
    case "accept":
      return map(tree, a.id, n => ({ ...n, answer: n.recommended, status: "answered" }))
    case "investigateInstead":
      // Convert an unanswerable clarifier into a promoted research sub-question.
      return map(tree, a.id, n => ({
        ...n, kind: "research", origin: "grill", status: "promoted",
        recommended: "", answer: "",
      }))
    case "togglePromote":
      return map(tree, a.id, n =>
        n.kind === "research"
          ? { ...n, status: n.status === "promoted" ? "open" : "promoted" }
          : n
      )
    case "prune": {
      const ids = subtreeIds(tree, a.id)
      return tree.map(n => (ids.has(n.id) ? { ...n, status: "pruned" } : n))
    }
    case "edit":
      return map(tree, a.id, n => ({ ...n, text: a.text }))
    default:
      return tree
  }
}

// --- Selectors ---

/** Depth-first order over non-pruned nodes (children grouped under each parent). */
function depthFirst(tree: GrillNode[]): GrillNode[] {
  const live = tree.filter(n => n.status !== "pruned")
  const byParent = new Map<string | null, GrillNode[]>()
  for (const n of live) {
    const k = n.parent
    ;(byParent.get(k) ?? byParent.set(k, []).get(k)!).push(n)
  }
  const out: GrillNode[] = []
  const walk = (parent: string | null) => {
    for (const n of byParent.get(parent) ?? []) {
      out.push(n)
      walk(n.id)
    }
  }
  walk(null)
  return out
}

/** Promoted research sub-questions, depth-first, as {text, origin}. */
export function promotedResearch(tree: GrillNode[]): { text: string; origin: string }[] {
  return depthFirst(tree)
    .filter(n => n.kind === "research" && n.status === "promoted" && n.text.trim())
    .map(n => ({ text: n.text.trim(), origin: n.origin || "grill" }))
}

/** Answered clarifiers as scope constraints {q, a}, excluding pruned/empty. */
export function answeredClarifiers(tree: GrillNode[]): { q: string; a: string }[] {
  return tree
    .filter(n => n.kind === "clarifier" && n.status === "answered" && n.answer.trim())
    .map(n => ({ q: n.text.trim(), a: n.answer.trim() }))
}

/** Depth of a node (root = 0); -1 if absent. Mirrors backend _node_depth. */
export function nodeDepth(tree: GrillNode[], id: string): number {
  const byId = new Map(tree.map(n => [n.id, n]))
  if (!byId.has(id)) return -1
  let depth = 0
  let cur = byId.get(id)!
  const seen = new Set<string>()
  while (cur && cur.parent && !seen.has(cur.id)) {
    seen.add(cur.id)
    depth += 1
    cur = byId.get(cur.parent)!
    if (!cur) break
  }
  return depth
}

/** Suggested max_cycles from committed sub-question count: N + ceil(N/3) + 1. */
export function suggestedMaxCycles(n: number): number {
  return n > 0 ? n + Math.ceil(n / 3) + 1 : 0
}
