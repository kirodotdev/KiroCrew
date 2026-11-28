/** The list's rows across loaded pages, each once: the first copy of a repeated id wins. */
export function firstOfEach<T>(rows: T[], id: (row: T) => string): T[] {
  const seen = new Set<string>()
  return rows.filter(row => { const key = id(row); if (seen.has(key)) return false; seen.add(key); return true })
}
