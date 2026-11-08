/** The row a list filter should open on its own: the first row with a name
 *  equal to the query, else the first row with a name that contains it, else
 *  the first row. The Skills, Steering and Prompts filters also match descriptions, so
 *  the first visible row can be one that merely mentions the query; opening
 *  that one hides the row the user typed (#20262). The list keeps its order. */
export function bestFilterMatch<T>(rows: T[], query: string, names: (row: T) => string[]): T | undefined {
  const q = query.trim().toLowerCase()
  if (!q) return rows[0]
  const lower = (row: T) => names(row).map(n => n.toLowerCase())
  return rows.find(r => lower(r).some(n => n === q))
    ?? rows.find(r => lower(r).some(n => n.includes(q)))
    ?? rows[0]
}
