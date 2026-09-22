# App Kit

Developer documentation for building apps that run inside Kiro Crew. For the
platform's own contracts (where an app's MCP servers land, how its agent JSON is
composed), see
[../system-specs/modules/app-kit-platform.md](../system-specs/modules/app-kit-platform.md).

| Document | Covers |
|---|---|
| [getting-started.md](getting-started.md) | Build your first app: scaffold, run, and iterate. |
| [manifest-reference.md](manifest-reference.md) | Every `app.json` field. |
| [api-reference.md](api-reference.md) | The App SDK surface: hooks, storage, chat embedding, and the event bus. |
| [publishing-guide.md](publishing-guide.md) | Publishing to the App Store, including the review guidelines an app must meet. |
| [migration-guide.md](migration-guide.md) | Moving an app across a breaking platform version. |

`examples/` holds two source fixtures: an installable minimal app and a fuller
UI/agent/skill/cron example. The full example intentionally omits build tooling,
a compiled UI bundle, and a backend; use it as a code reference rather than
installing it directly.
