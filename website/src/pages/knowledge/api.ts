export async function knowledgeApi<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`/api/knowledge${path}`, opts)
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`
    let code: string | undefined
    try {
      const body = await r.json()
      if (body?.error) msg = body.error
      if (typeof body?.code === 'string') code = body.code
    } catch { /* non-JSON body — keep status line */ }
    // ``code`` is the machine-readable reason, so a caller can show localized text.
    throw Object.assign(new Error(msg), code ? { code } : {})
  }
  return r.json()
}
