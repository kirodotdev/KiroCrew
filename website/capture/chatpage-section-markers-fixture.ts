/**
 * The one fixture both the capture entry and its script read, so the page's
 * mount refetch answers with exactly the rows the store was preloaded with.
 */
export type Msg = {
  role: string
  content: string
  cls: string
  ts: string
  meta?: Record<string, unknown>
}

export const markerFixture = (): Msg[] => {
  const today = new Date()
  const at = (h: number, m: number, s: number): string => {
    const d = new Date(today)
    d.setHours(h, m, s, 0)
    return d.toISOString()
  }
  return [
    { role: 'user', content: 'Review item-41: the retry budget change.', cls: '', ts: at(14, 2, 0) },
    { role: 'assistant', content: 'item-41 is done.\n\n- the budget now resets per turn\n- added a test for the reset', cls: '', ts: at(14, 9, 40) },
    { role: 'section_marker', content: '— End of section: item-41 —', cls: '', ts: at(14, 9, 41), meta: { label: 'item-41' } },
    { role: 'user', content: 'Next: item-42, the resolver rename.', cls: '', ts: at(14, 10, 5) },
    { role: 'assistant', content: 'item-42 is done.\n\n- renamed the resolver\n- updated both call sites', cls: '', ts: at(14, 18, 31) },
    { role: 'section_marker', content: '— End of section —', cls: '', ts: at(14, 18, 32), meta: { label: '' } },
    { role: 'user', content: 'Start on item-43.', cls: '', ts: at(14, 19, 0) },
    { role: 'assistant', content: 'Starting on item-43 now.', cls: '', ts: at(14, 19, 12) },
  ]
}
