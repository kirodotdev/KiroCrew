# Google Calendar card: Google notes

Set up Google once for every Google card with [google.md](google.md): run the script, three clicks, paste once. This page keeps only what is specific to Google Calendar.

| | |
|---|---|
| Redirect URI | `http://127.0.0.1:48105/callback` |
| MCP endpoint | `https://calendarmcp.googleapis.com/mcp/v1` |
| APIs the script enables | `calendar-json.googleapis.com` and `calendarmcp.googleapis.com` |
| Environment variable prefix | `KIROCREW_CONNECTIONS_GOOGLE_CALENDAR_` (`CLIENT_ID`, `CLIENT_SECRET`) |

## Scopes

| Scope | Kiro Crew requests it | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/calendar.calendarlist.readonly` | yes | Read your calendar list | `list_calendars` |
| `https://www.googleapis.com/auth/calendar.events.readonly` | yes | Read events | `list_events`, `get_event`, `search_events` |
| `https://www.googleapis.com/auth/calendar.events.freebusy` | yes | Read free/busy | `suggest_time` |

Google's page does not map tools to scopes; the mapping follows each scope's description. Scopes on **Data Access** are a ceiling; the consent screen shows what Kiro Crew actually asks for.

## Check it works

After **Connect** shows **Connected**, ask in a chat: "When is my next meeting with Ariel?" (use a real attendee). Expect `list_events`.

Tools the server lists: `create_event`, `delete_event`, `get_event`, `list_calendars`, `list_events`, `respond_to_event`, `search_events`, `suggest_time`, `update_event`.

## Gotchas

- None of these scopes is restricted, so publishing would never need the security assessment.
- Google lists write tools (`create_event`, `update_event`, `delete_event`, `respond_to_event`) but documents no write scope for them. They fail with a permission error; that is expected. Kiro Crew asks Google only for the read-only scopes above, so adding more on **Data Access** does not change what Connect grants.
- Event descriptions and invitations are untrusted input; review what agents do with them.

## Sources

- https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server
- https://developers.google.com/workspace/guides/configure-mcp-servers
