/**
 * What fills <main> while a relay Crew's pane hides under a local menu or
 * dialog: the Crew's name, so the area under the overlay is not an empty
 * window. RelayCrewPaneHost shows it in the box that is exactly <main>. The name
 * is the tab's own label, already on screen in the tab bar, so it is hidden
 * from assistive technology here.
 */
export default function RelayPaneUnderOverlayCover({ name }: { name: string }) {
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-bg" aria-hidden="true" data-testid="relay-crew-under-overlay">
      <div className="max-w-md truncate text-sm font-medium text-muted">{name}</div>
    </div>
  )
}
