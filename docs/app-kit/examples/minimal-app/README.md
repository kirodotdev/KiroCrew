# Hello World — Minimal Kiro Crew App

The simplest possible Kiro Crew app: one agent + one skill, no UI.

## Install

```bash
kirocrew app install ./minimal-app
```

Trust the app before you enable it: enable refuses every app that is not built
in, this one included, until it is trusted. Enable it once from the dashboard's
App Store, which asks you to trust this app alone (`agent.apps_trusted`).
Settings → Security → Third-party apps lists and revokes those grants, and holds
the switch that trusts every third-party app (`agent.apps_allow_third_party`).
After that, the command below works too. See
[Third-party executable code is off by default](../../publishing-guide.md#third-party-executable-code-is-off-by-default).

```bash
kirocrew app enable hello-world
```

## Use

Chat with the `greeter` agent from any Kiro Crew chat after enabling the app.
