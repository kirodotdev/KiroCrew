"""Command-line entry point for Kiro BYO."""

from __future__ import annotations

import argparse
import json
import os

from . import __version__
from .acp_server import AcpServer, run_acp
from .config import ConfigError, config_path, load_config
from .credential_store import CredentialStoreError, default_credential_store
from .discovery import DiscoveryError, discover_models
from .paths import crew_data_home
from .provider_admin import configure_provider, migrate_plaintext_credentials


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiro-byo", description="Provider-neutral ACP Harness for KiroCrew")
    parser.add_argument("--version", action="version", version=f"kiro-byo {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("acp", help="serve ACP v1 over stdin/stdout")
    sub.add_parser("config-path", help="print the active providers.json path")
    sub.add_parser("models", help="list models currently advertised by Kiro BYO")

    discover = sub.add_parser("discover-models", help="discover models from an OpenAI-compatible /models endpoint")
    discover.add_argument("--base-url", required=True)
    discover.add_argument("--api-key-env", default="KIRO_BYO_SETUP_KEY")
    discover.add_argument("--json", action="store_true", dest="as_json")

    configure = sub.add_parser("configure-provider", help="discover and save a provider using the OS credential store")
    configure.add_argument("--base-url", required=True)
    configure.add_argument("--api-key-env", default="KIRO_BYO_SETUP_KEY")
    configure.add_argument("--name", default="")
    configure.add_argument("--preset", default="custom")
    configure.add_argument("--instance-id", default="")
    configure.add_argument("--new-instance", action="store_true")
    configure.add_argument("--json", action="store_true", dest="as_json")

    migrate = sub.add_parser("migrate-credentials", help="move plaintext provider secrets into the OS credential store")
    migrate.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("credential-status", help="print the selected OS credential backend without revealing secrets")
    sub.add_parser("paths", help="print resolved Kiro BYO and Kiro Crew data paths")

    ui = sub.add_parser("ui", help="open the local provider/credential management UI")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "acp":
        return run_acp()
    if args.command == "config-path":
        print(config_path())
        return 0
    if args.command == "models":
        try:
            config = load_config()
        except ConfigError as exc:
            parser.error(str(exc))
        for key, name in AcpServer.available_models(config):
            print(f"{key}\t{name}")
        return 0
    if args.command == "discover-models":
        api_key = os.environ.get(args.api_key_env, "")
        try:
            provider_id, provider_name, models = discover_models(args.base_url, api_key)
        except DiscoveryError as exc:
            message = str(exc)
            if api_key:
                message = message.replace(api_key, "[REDACTED]")
            parser.error(message)
        payload = {
            "provider_id": provider_id,
            "provider_name": provider_name,
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "context_window": m.context_window,
                    "max_output_tokens": m.max_output_tokens,
                    "input_modalities": list(m.input_modalities),
                    "output_modalities": list(m.output_modalities),
                    "effort_supported_levels": list(m.effort_supported_levels),
                    "effort_default_level": m.effort_default_level,
                    "reasoning": {
                        "mode": m.reasoning_mode,
                        "supported": m.reasoning_supported,
                        "can_disable": m.reasoning_can_disable,
                        "default_enabled": m.reasoning_default_enabled,
                        "disable_value": m.reasoning_disable_value,
                        "enable_value": m.reasoning_enable_value,
                        "auto_value": m.reasoning_auto_value,
                        "supports_auto": m.reasoning_supports_auto,
                        "thinking": {
                            "supported": m.reasoning_supported,
                            "can_disable": m.reasoning_can_disable,
                            "default_enabled": m.reasoning_default_enabled,
                            "disable_value": m.reasoning_disable_value,
                            "enable_value": m.reasoning_enable_value,
                            "auto_value": m.reasoning_auto_value,
                            "supports_auto": m.reasoning_supports_auto,
                        },
                        "effort": {
                            "levels": list(m.effort_supported_levels),
                            "default": m.effort_default_level,
                            "fixed_level": m.reasoning_fixed_level,
                        },
                        "levels": list(m.effort_supported_levels),
                        "default": m.effort_default_level,
                        "fixed_level": m.reasoning_fixed_level,
                        "source": m.capability_source,
                    },
                }
                for m in models
            ],
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"{provider_name} ({provider_id})")
            for model in models:
                print(f"{model.id}\t{model.name}")
        return 0
    if args.command == "configure-provider":
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            parser.error(f"environment variable {args.api_key_env} is empty")
        try:
            result = configure_provider(
                base_url=args.base_url,
                api_key=api_key,
                provider_name=args.name,
                preset_id=args.preset,
                instance_id=args.instance_id,
                create_new=args.new_instance,
            )
        except (ConfigError, CredentialStoreError, DiscoveryError) as exc:
            parser.error(str(exc))
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Saved provider {result['name']} ({result['id']})")
            print(f"Credential: configured via {default_credential_store().backend_name}")
            print(f"Models: {result['model_count']}")
        return 0
    if args.command == "migrate-credentials":
        try:
            report = migrate_plaintext_credentials()
        except (ConfigError, CredentialStoreError) as exc:
            parser.error(str(exc))
        payload = {
            "changed": report.changed,
            "migrated_providers": list(report.migrated_providers),
            "migrated_secret_headers": report.migrated_secret_headers,
            "sanitized_backup_files": list(report.sanitized_backup_files),
            "credential_backend": report.credential_backend,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"Credential backend: {report.credential_backend}")
            print(f"Migrated providers: {len(report.migrated_providers)}")
            print(f"Migrated secret headers: {report.migrated_secret_headers}")
            print(f"Sanitized backup files: {len(report.sanitized_backup_files)}")
            print(f"Config changed: {report.changed}")
        return 0
    if args.command == "credential-status":
        try:
            store = default_credential_store()
        except CredentialStoreError as exc:
            parser.error(str(exc))
        print(store.backend_name)
        return 0
    if args.command == "paths":
        crew_home = crew_data_home()
        print(f"config={config_path()}")
        print(f"kiro_crew_data={crew_home if crew_home is not None else '<unknown>'}")
        return 0
    if args.command == "ui":
        try:
            from .web_ui import serve_ui
            serve_ui(port=args.port, open_browser=not args.no_browser)
        except (CredentialStoreError, OSError) as exc:
            parser.error(str(exc))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
