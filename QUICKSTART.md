**注意：0.2.4 仍是兼容候选版。安装前应备份数据；若 0.11.0-rc.1 源码校验不通过，安装器将拒绝修改 Crew。当前不支持旧式全局队友联动写入。**

# Kiro BYO 0.2.4 — Crew 0.11.0-rc.1 candidate

Kiro BYO is packaged as a KiroCrew App and declares **macOS, Linux and Windows**.
The dashboard/Chat compatibility adapter is shared across platforms: it discovers the
active imported `kiro_crew` package and validates authored/semantic contracts instead of
hard-coding an OS install directory or a content-hashed Vite asset name.

## Install / update

Install the folder or zip containing `app.json`, then open:

```text
Kiro BYO → Settings → KiroCrew → Install / Update integration
```

The preflight is fail-closed. It checks the installed KiroCrew source seams and dashboard
contracts before writing anything. Completely restart KiroCrew after a successful
integration, then switch **Settings → Agent Harness → Kiro BYO**. Only cold chat slots (those without a running Provider) are reconciled to the
configured default Harness. Live sessions remain owned by the Provider with which they
were opened; explicit concrete model pins remain unchanged.

## Provider credentials

The same Provider UI is used on every platform. Live secrets are kept out of provider
JSON and routed to the native credential backend:

- Windows: Credential Manager
- macOS: Keychain
- Linux: Secret Service (`secret-tool`; `env:VARIABLE` references remain available on
  minimal/headless hosts)

## Historical 0.10 Chat picker implementation (source contract revalidation required on 0.11)

KiroCrew 0.10 shares one React-Query model catalogue between Chat and non-Chat
surfaces. Kiro BYO no longer patches a compiled `withJevRoute`/Auto(Jev) helper and no
longer changes `/api/models` by route. While BYO owns the harness, the endpoint has one
stable shape: the active Mode's Type sentinels followed by concrete provider models. Chat
uses those Type rows as routing selections; the dashboard presentation guard hides them
outside the interactive Chat picker. No Kiro BYO navigation hook calls `reload()` or
replaces `window.fetch`, so all KiroCrew pages retain native SPA navigation.

When a harness switch leaves an already-open slot carrying native Jev state
(`model=auto` plus `jev_route=true`), the 0.10 adapter reconciles that routing-only state to
the active BYO default Type and pushes the slot update immediately. Explicit concrete
model pins are not overwritten; switching away from BYO also removes any BYO-private Type
sentinel before another harness sees it.

The dashboard integration therefore depends only on the authored `data-model-id` DOM
contract and `index.html` shell. It does not depend on `App-DBY44uc_.js`, Vite chunk
names, minified helper syntax, asset hashes, drive letters, or operating-system paths.
The same adapter is used on Windows, macOS and Linux.

`WINDOWS_QUICKSTART.md` contains additional Windows-specific repair notes.
