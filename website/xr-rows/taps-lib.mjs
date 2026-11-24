// Round 15, TAPS-2: what the taps' probes share.
//
// `pinBundle(context)`: the shared dev gateway's dist link is pointed back and forth between two built dists while
// other rows run (TAPS-2.md, part 3). Each change moves the `bundle_id` in the gateway's status frames, and the tab
// then loads itself again (`hooks/websocket/bundleReload.ts`), which ends a session under a probe. The probe's tab
// is given one constant id instead: every frame from the gateway passes through with its `bundle_id` rewritten.
// Nothing is changed at the gateway, and nothing else in a frame is touched.
export async function pinBundle(context, log = null) {
  await context.routeWebSocket(/\/api\/ws(\?|$)/, ws => {
    const server = ws.connectToServer()
    server.onMessage(m => {
      if (typeof m !== 'string') { ws.send(m); return }
      const id = /"bundle_id"\s*:\s*"([^"]*)"/.exec(m)?.[1]
      if (id !== undefined && log) log(id)
      ws.send(id === undefined ? m : m.replace(/"bundle_id"\s*:\s*"[^"]*"/g, '"bundle_id":"pinned-by-the-probe"'))
    })
  })
}
