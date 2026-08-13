export async function knowledgeApi<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`/api/knowledge${path}`, opts)
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`
    try {
      const body = await r.json()
      if (body?.error) msg = body.error
    } catch { /* non-JSON body — keep status line */ }
    throw new Error(msg)
  }
  return r.json()
}
