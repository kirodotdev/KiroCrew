Real-component captures with isolated fixture state and mocked APIs show the
neutral cleanup notice beside the queue, with an hourglass glyph, saying the
message sends once the turn stops, and the card's lightning button disabled
until teardown ends.

The held-queue captures follow a "Send now" press the server answered with
`queue_held`. The teardown captures have no press: the slot reports
`stop_teardown` and the pointer hovers the disabled lightning button. Its
`title` is "Still stopping…" in the DOM, but headless Chromium does not paint
native tooltips, and touch screens never show them, so the notice is the
visible reason in both states.
These captures do not prove live gateway or provider behavior.

![ChatPane held queue](temp-screenshots/queue-interrupt-notice/chatpane-held-queue.png)
![ChatPage held queue](temp-screenshots/queue-interrupt-notice/chatpage-held-queue.png)
![ChatPane mobile held queue](temp-screenshots/queue-interrupt-notice/chatpane-held-queue-mobile.png)
![ChatPage mobile held queue](temp-screenshots/queue-interrupt-notice/chatpage-held-queue-mobile.png)
![ChatPane teardown, disabled Send now hovered](temp-screenshots/queue-interrupt-notice/chatpane-teardown-hover.png)
![ChatPage teardown, disabled Send now hovered](temp-screenshots/queue-interrupt-notice/chatpage-teardown-hover.png)
![ChatPane mobile teardown without a press](temp-screenshots/queue-interrupt-notice/chatpane-teardown-mobile.png)
![ChatPage mobile teardown without a press](temp-screenshots/queue-interrupt-notice/chatpage-teardown-mobile.png)
