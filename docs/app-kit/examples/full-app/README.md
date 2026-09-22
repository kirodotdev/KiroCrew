# Oncall Watchtower — Full Example Source

Illustrates a Kiro Crew UI page, agent, skill, five-minute cron declaration,
real-time event subscription, and navigation badge.

This directory is a source fixture, not a directly runnable app. Its `ui/`
directory contains only `src/App.tsx`: there is no `package.json`, Vite config,
compiled `dist/index.mjs`, or backend for the component's `/api/tickets` call.

## Use as a reference

Start from a complete scaffold, then port the UI component, agent, skill, and
manifest fields you need:

```bash
kirocrew app init oncall-watchtower --ui --cron
```

Add an app backend that serves `/api/tickets`, build the UI, and follow the
install and enable steps in [Getting Started](../../getting-started.md).
