"""``contributes.widgets`` on the shared app frame (``apps/app_frame.py``).

An agent draws an app widget with ``<mcwidget use="<app>:<id>">{json}</mcwidget>``.
The dashboard mints this point's frame for the declared widget and sends the parsed
payload of that one tag in ``widget.render``. Nothing else from the conversation
reaches the frame: not the transcript, not the session, not the message around the
tag.

The point is ``offline`` (``FramePoint.offline``): the mint gives the frame no
capability, so its module has no ``ctx.fetch`` and reaches no backend, and its
policy loads no network image. The payload is agent output, so an image URL the
module built from it would otherwise carry conversation text to any host.

This module says which entry a frame loads (a declared widget of an enabled app) and
what the frame script hands the app's module. It is registered at the bottom of
``app_frame.py``.
"""

from __future__ import annotations

from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

#: The manifest key, which is the point's name in the frame routes.
WIDGETS_POINT = "widgets"


def resolve_widget(name: str, widget_id: str) -> tuple[str, bool, str] | None:
    """``(entry, process_backed, app)`` of an enabled app's declared widget, else None."""
    # Imported here: app_frame imports this module at its bottom to register it.
    from kiro_crew.apps.app_frame import clean_entry, process_backed

    if not is_app_enabled(name):
        return None
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name:
        return None
    widget = manifest.contributes.widgets.by_id(widget_id)
    if widget is None:
        return None
    entry = clean_entry(widget.entry)
    if entry is None or "\\" in entry:
        return None
    return entry, process_backed(manifest), manifest.name


# A widget. The host sends `widget.render` once per frame with the parsed payload;
# the handler draws it and reports its height through `view.resize`. A second
# render is ignored: the payload of a finished tag does not change. `ctx` carries no
# `fetch`: the point is offline, so the frame holds no capability.
WIDGET_SCRIPT = r"""
let rendered = false;
const ctx = Object.freeze({
  appName: CFG.app,
  point: 'widgets',
  theme: () => CFG.theme,
});
const POINT = {
  ctx,
  accepts: (h) => !!h && typeof h.render === 'function',
  missing: 'activate(ctx) returned no render(props, view)',
  message(m) {
    if (m.type !== 'widget.render' || rendered || !handler) return;
    if (!m.props || typeof m.props !== 'object' || Array.isArray(m.props)) return;
    rendered = true;
    CFG.theme = m.theme === 'dark' ? 'dark' : 'light';
    const view = Object.freeze({
      widgetId: typeof m.widgetId === 'string' ? m.widgetId : '',
      theme: CFG.theme,
      resize: (size) => send('widget.resize', { height: clampNum(size && size.height) }),
    });
    Promise.resolve()
      .then(() => handler.render(m.props, view))
      .catch((err) => send('failed', { message: String((err && err.message) || err).slice(0, 200) }));
  },
};
"""
