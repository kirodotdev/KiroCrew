# Completions & Autocomplete

Source: https://kiro.dev/docs/cli/autocomplete/

Two AI-powered features: autocomplete dropdown menu and inline ghost-text suggestions. Both support hundreds of CLI tools (git, npm, docker, aws, etc.).

## Autocomplete dropdown

Appears automatically as you type. Arrow keys to navigate, Tab/Enter to select.

```bash
kiro-cli settings autocomplete.disable false   # enable
kiro-cli settings autocomplete.disable true    # disable
kiro-cli theme dark|light|system               # change theme
```

## Inline suggestions

Gray ghost text on your command line. Right arrow or Tab to accept.

```bash
kiro-cli inline enable
kiro-cli inline disable
kiro-cli inline status
kiro-cli inline set-customization [ARN]
kiro-cli inline show-customizations
```

## Supported tools

- **Popular**: git, docker, npm/yarn, kubectl, terraform, aws
- **Language**: pip/poetry/conda, npm/yarn/pnpm, gem/bundle, go
- **System**: Unix commands, package managers (apt, brew, yum), file operations
