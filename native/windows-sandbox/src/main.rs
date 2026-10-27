//! Native Windows-only confinement runner. No Codex config or legacy fallback.
//! Uses the same pinned Microsoft MXC SDK as Codex rust-v0.164.0.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, Prefix};
use std::process;

use mxc_sdk::mxc_common::cmdline::{CommandLineContext, cmdline_from_argv_for_context};
use mxc_sdk::mxc_common::filesystem_object::{
    ExistingObjectComparison, compare_existing_filesystem_objects, normalize_object_conflicts,
};
use mxc_sdk::mxc_common::logger::{Logger, Mode};
use mxc_sdk::mxc_common::models::{
    BaseProcessUiConfig, ContainerPolicy, ExecutionRequest, FallbackPolicy, NetworkAction,
    NetworkEgressPolicy, NetworkIngressPolicy, ScriptResponse, UiPolicy,
};
use mxc_sdk::mxc_common::sandbox_process::{SandboxBackend, StdioMode};
use mxc_sdk::process_container_common::base_container_runner::BaseContainerRunner;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use windows_sys::Win32::Storage::FileSystem::{
    BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileInformationByHandle, GetLogicalDrives,
};

// tracelogging 1.2.3 requires its documented ETW import library on Windows GNU.
#[link(name = "advapi32")]
unsafe extern "system" {}

const MAX_POLICY_BYTES: u64 = 1_048_576;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum Access {
    Read,
    Write,
    Deny,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Policy {
    version: u32,
    filesystem: BTreeMap<String, Access>,
    network: bool,
    #[serde(default)]
    process_descriptor: Option<ProcessDescriptor>,
    // The isolated Python launcher consumes this metadata. Keeping it in the
    // authenticated policy preserves the caller's single-file cleanup contract.
    #[serde(default, rename = "launch")]
    _launch: Option<serde::de::IgnoredAny>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProcessDescriptor {
    path: String,
    instance: String,
}

fn prepare_process_descriptor(source: &Policy) -> Result<Option<File>, Failure> {
    let Some(descriptor) = &source.process_descriptor else {
        return Ok(None);
    };
    if !absolute_local(&descriptor.path)
        || source.filesystem.get(&descriptor.path) != Some(&Access::Read)
        || descriptor.instance.len() != 64
        || !descriptor.instance.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(Failure::Policy);
    }
    // Create before PSEC resolves the exact READ object. Keep its identity
    // pinned (no SHARE_DELETE) until the workload/job exits. It contains no key.
    OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&descriptor.path)
        .map(Some)
        .map_err(|_| Failure::Filesystem)
}

struct Invocation {
    policy_path: String,
    digest: String,
    cwd: String,
    command: Vec<String>,
}

#[derive(Debug)]
enum Failure {
    Policy,
    Input,
    Scope,
    Arguments,
    Filesystem,
    Native,
}

fn absolute_local(path: &str) -> bool {
    if path.contains('\0') || path.get(2..).is_none_or(|tail| tail.contains(':')) {
        return false;
    }
    let value = Path::new(path);
    value.is_absolute()
        && matches!(
            value.components().next(),
            Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_))
        )
}

fn invocation(args: Vec<String>) -> Result<Invocation, Failure> {
    if args.len() < 8
        || args[0] != "--policy-file"
        || args[2] != "--policy-sha256"
        || args[4] != "--cwd"
        || args[6] != "--"
        || !absolute_local(&args[1])
        || !absolute_local(&args[5])
    {
        return Err(Failure::Policy);
    }
    Ok(Invocation {
        policy_path: args[1].clone(),
        digest: args[3].clone(),
        cwd: args[5].clone(),
        command: args[7..].to_vec(),
    })
}

fn read_policy(input: &Invocation) -> Result<(Policy, File), Failure> {
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&input.policy_path)
        .map_err(|_| Failure::Policy)?;
    let metadata = file.metadata().map_err(|_| Failure::Policy)?;
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err(Failure::Policy);
    }
    if !metadata.is_file()
        || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || information.nNumberOfLinks != 1
        || metadata.len() > MAX_POLICY_BYTES
    {
        return Err(Failure::Policy);
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(MAX_POLICY_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Failure::Policy)?;
    if bytes.len() as u64 > MAX_POLICY_BYTES
        || format!("{:x}", Sha256::digest(&bytes)) != input.digest
    {
        return Err(Failure::Policy);
    }
    let policy: Policy = serde_json::from_slice(&bytes).map_err(|_| Failure::Policy)?;
    if policy.version != 1 || policy.filesystem.is_empty() {
        return Err(Failure::Policy);
    }
    Ok((policy, file))
}

fn grant(policy: &mut ContainerPolicy, path: String, access: Access) {
    match access {
        Access::Read => policy.readonly_paths.push(path),
        Access::Write => policy.readwrite_paths.push(path),
        Access::Deny => policy.denied_paths.push(path),
    }
}

fn normalize(policy: ContainerPolicy, logger: &mut Logger) -> Result<ContainerPolicy, Failure> {
    normalize_object_conflicts(&policy, logger)
        .map(|normalized| normalized.unwrap_or(policy))
        .map_err(|_| Failure::Filesystem)
}

fn expand_volumes(paths: Vec<String>) -> Result<Vec<String>, Failure> {
    let mut expanded = Vec::new();
    for path in paths {
        let root = Path::new(&path);
        if root.parent().is_none() {
            match std::fs::read_dir(root) {
                Ok(children) => {
                    for child in children {
                        let child = child.map_err(|_| Failure::Filesystem)?.path();
                        if compare_existing_filesystem_objects(&child, &child)
                            == ExistingObjectComparison::Same
                        {
                            expanded.push(child.to_str().ok_or(Failure::Filesystem)?.to_owned());
                        }
                    }
                }
                Err(error)
                    if matches!(
                        error.raw_os_error(),
                        Some(2 | 3 | 5 | 15 | 21 | 55 | 67 | 1117)
                    ) =>
                {
                    continue;
                }
                Err(_) => return Err(Failure::Filesystem),
            }
        }
        expanded.push(path);
    }
    Ok(expanded)
}

fn base_policy(network: bool) -> ContainerPolicy {
    ContainerPolicy {
        capabilities: vec!["registryRead".into()],
        fallback: FallbackPolicy {
            allow_dacl_mutation: false,
        },
        network_egress: Some(NetworkEgressPolicy {
            default: if network {
                NetworkAction::Allow
            } else {
                NetworkAction::Deny
            },
            ..Default::default()
        }),
        network_ingress: Some(NetworkIngressPolicy {
            default: NetworkAction::Deny,
            host_loopback: if network {
                NetworkAction::Allow
            } else {
                NetworkAction::Deny
            },
        }),
        network_specified: true,
        network_mode_specified: true,
        ui: UiPolicy {
            disable: false,
            ..Default::default()
        },
        base_process_ui: BaseProcessUiConfig {
            isolation: "desktop".into(),
            ..Default::default()
        },
        ..Default::default()
    }
}

fn container_policy(source: Policy, logger: &mut Logger) -> Result<ContainerPolicy, Failure> {
    let mut policy = base_policy(source.network);
    let mut entries = source.filesystem;
    let root_access = entries.remove(":root").ok_or(Failure::Policy)?;
    if root_access == Access::Deny {
        return Err(Failure::Policy);
    }
    let drives = unsafe { GetLogicalDrives() };
    if drives == 0 {
        return Err(Failure::Native);
    }
    for letter in 0..26 {
        if drives & (1 << letter) != 0 {
            let root = format!("{}:\\", char::from(b'A' + letter));
            // GetLogicalDrives includes disconnected/removable/virtual roots.
            // These are generated grants, so omit inaccessible roots. Explicit
            // policy paths remain subject to fail-closed object normalization.
            if Path::new(&root).is_dir() {
                grant(&mut policy, root, root_access);
            }
        }
    }
    for (path, access) in entries {
        if !absolute_local(&path) {
            return Err(Failure::Policy);
        }
        // A volume-wide deny is not a validated Crew renderer capability.
        if access == Access::Deny && Path::new(&path).parent().is_none() {
            return Err(Failure::Policy);
        }
        grant(&mut policy, path, access);
    }
    // Tighten equivalent object spellings before expanding nonrecursive volume
    // grants, and again after expansion. Never restore a generated write alias.
    policy = normalize(policy, logger)?;
    policy.readwrite_paths = expand_volumes(policy.readwrite_paths)?;
    policy.readonly_paths = expand_volumes(policy.readonly_paths)?;
    normalize(policy, logger)
}

fn startup_detail(response: &ScriptResponse) -> (&'static str, Option<u32>, Option<u32>) {
    let message = &response.error_message;
    let detail = if message.starts_with("failed to create the process security environment: ") {
        message.as_str()
    } else if message.starts_with("failed to attach the process security environment: ") {
        message.as_str()
    } else {
        response.extended_error.as_str()
    };
    let operation = [
        "CreateProcessSecurityEnvironment",
        "UpdateProcThreadAttribute",
        "CreateProcessW",
        "AssignProcessToJobObject",
        "ResumeThread",
    ]
    .into_iter()
    .find(|name| {
        detail.starts_with(name)
            || (detail == message && detail.contains(&format!("{name} failed (")))
    })
    .unwrap_or("not-reported");
    if operation == "not-reported" {
        return (operation, None, None);
    }
    let win32 = detail
        .split("WIN32_ERROR(")
        .nth(1)
        .or_else(|| detail.split("Win32 error = ").nth(1))
        .and_then(|tail| tail.split(')').next())
        .and_then(|digits| digits.parse::<u32>().ok());
    let hresult = detail
        .split("HRESULT = 0x")
        .nth(1)
        .and_then(|tail| tail.get(..8))
        .and_then(|digits| u32::from_str_radix(digits, 16).ok());
    (operation, win32, hresult)
}

fn report_spawn_failure(response: ScriptResponse) -> Failure {
    // The SDK includes typed PSEC errors in error_message, not extended_error.
    // Emit only a known API name and parsed numeric codes, never its raw text.
    let (operation, win32, hresult) = startup_detail(&response);
    let category = [
        ("network.ingress", "network-ingress"),
        ("schema version", "psec-schema"),
        ("stdin", "stdin"),
        ("stdout", "stdout"),
        ("stderr", "stderr"),
        ("environment", "environment"),
        ("job", "job"),
    ]
    .into_iter()
    .find(|(needle, _)| response.error_message.contains(needle))
    .map(|(_, category)| category)
    .unwrap_or("native-start");
    let hresult = hresult.map(|code| format!("0x{code:08X}"));
    eprintln!(
        "native MXC startup detail: phase={:?}; operation={}; win32={:?}; hresult={:?}; category={}",
        response.failure_phase, operation, win32, hresult, category
    );
    Failure::Native
}

fn run(input: Invocation) -> Result<i32, Failure> {
    let (source, _pinned_policy) = read_policy(&input).map_err(|_| Failure::Input)?;
    if !BaseContainerRunner::is_base_container_api_present()
        || !BaseContainerRunner::supports_native_denied_paths()
    {
        return Err(Failure::Native);
    }
    let mut descriptor_file = prepare_process_descriptor(&source)?;
    let descriptor_instance = source
        .process_descriptor
        .as_ref()
        .map(|d| d.instance.clone());
    let mut logger = Logger::new(Mode::Buffer);
    let policy = container_policy(source, &mut logger).map_err(|_| Failure::Scope)?;
    let environment = std::env::vars_os()
        .map(|(key, value)| {
            Ok(format!(
                "{}={}",
                key.into_string().map_err(|_| Failure::Policy)?,
                value.into_string().map_err(|_| Failure::Policy)?,
            ))
        })
        .collect::<Result<Vec<_>, Failure>>()?;
    let request = ExecutionRequest {
        script_code: cmdline_from_argv_for_context(
            &input.command,
            CommandLineContext::WindowsCreateProcess,
        )
        .map_err(|_| Failure::Policy)?,
        working_directory: input.cwd,
        env: Some(environment),
        inherit_default_env: false,
        policy,
        ..Default::default()
    };
    let executable_kind = match Path::new(&input.command[0])
        .extension()
        .and_then(|s| s.to_str())
    {
        Some(extension) if extension.eq_ignore_ascii_case("exe") => "exe",
        Some(extension)
            if extension.eq_ignore_ascii_case("cmd") || extension.eq_ignore_ascii_case("bat") =>
        {
            "cmd"
        }
        _ => "other",
    };
    let mut child = BaseContainerRunner::new()
        .spawn(&request, &mut logger, StdioMode::Inherit)
        .map_err(|error| {
            eprintln!("native MXC command kind: {}", executable_kind);
            report_spawn_failure(error)
        })?;
    if let Some(file) = descriptor_file.as_mut() {
        let record = serde_json::json!({"pid": child.id(), "instance": descriptor_instance});
        let published = file
            .write_all(record.to_string().as_bytes())
            .and_then(|_| file.sync_all());
        if published.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Failure::Filesystem);
        }
    }
    // The SDK diagnostic buffer may contain argv/environment. Never emit it.
    child.wait().map_err(|_| Failure::Native)
}

fn main() {
    let args = std::env::args_os()
        .skip(1)
        .map(|value| value.into_string().map_err(|_| Failure::Policy))
        .collect::<Result<Vec<_>, _>>();
    if let Ok(args) = &args
        && args == &["--version"]
    {
        println!(
            "kirocrew-windows-sandbox 0.1.0 protocol=1 mxc=b21120168d70befe12356411423266bfef168446"
        );
        return;
    }
    if let Ok(args) = &args
        && args == &["--capabilities-json"]
    {
        println!(
            "{}",
            serde_json::json!({
                "protocol": 1,
                "native_api": BaseContainerRunner::is_base_container_api_present(),
                "deny_paths": BaseContainerRunner::supports_native_denied_paths(),
                "host_loopback": BaseContainerRunner::supports_ingress_host_loopback_allow(),
            })
        );
        return;
    }
    match args
        .and_then(|args| invocation(args).map_err(|_| Failure::Arguments))
        .and_then(run)
    {
        Ok(code) => process::exit(code),
        Err(Failure::Policy) => {
            eprintln!("Kiro Crew native Windows policy refused");
            process::exit(125);
        }
        Err(Failure::Input) => {
            eprintln!("Kiro Crew native policy file refused");
            process::exit(125);
        }
        Err(Failure::Scope) => {
            eprintln!("Kiro Crew native filesystem policy unavailable");
            process::exit(125);
        }
        Err(Failure::Arguments) => {
            eprintln!("Kiro Crew native invocation refused");
            process::exit(125);
        }
        Err(Failure::Filesystem) => {
            eprintln!("Kiro Crew native filesystem policy unavailable");
            process::exit(125);
        }
        Err(Failure::Native) => {
            eprintln!("native MXC is unavailable on this executor");
            process::exit(126);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_policy_fields_are_rejected() {
        assert!(
            serde_json::from_str::<Policy>(
                r#"{"version":1,"filesystem":{":root":"write"},"network":true,"fallback":true}"#
            )
            .is_err()
        );
    }

    #[test]
    fn launch_metadata_does_not_supply_native_permissions() {
        let policy = serde_json::from_str::<Policy>(
            r#"{"version":1,"filesystem":{":root":"read"},"network":false,"launch":{"filesystem":{":root":"write"}}}"#,
        ).unwrap();
        assert_eq!(policy.filesystem[":root"], Access::Read);
        assert!(!policy.network);
    }

    #[test]
    fn paths_are_literal_local_absolute_paths() {
        assert!(absolute_local(r"C:\Users\tester\secrets"));
        assert!(!absolute_local(r"C:relative"));
        assert!(!absolute_local(r"\\server\share"));
        assert!(!absolute_local(r"\\?\C:\device"));
        assert!(!absolute_local(r"C:\sealed:$I30:$INDEX_ALLOCATION\child"));
        assert!(!absolute_local(r"C:\sealed::$INDEX_ALLOCATION\child"));
        assert!(!absolute_local(r"C:\secret:stream"));
    }

    #[test]
    fn arguments_cannot_drop_policy_or_splice_native_flags() {
        assert!(invocation(vec!["cmd.exe".into(), "/c".into(), "echo".into()]).is_err());
        let input = invocation(vec![
            "--policy-file".into(),
            r"C:\run\policy.json".into(),
            "--policy-sha256".into(),
            "digest".into(),
            "--cwd".into(),
            r"C:\work".into(),
            "--".into(),
            "node.exe".into(),
            "a & b".into(),
        ])
        .unwrap();
        assert_eq!(input.command, ["node.exe", "a & b"]);
    }

    #[test]
    fn fallback_never_mutates_host_acls() {
        let compiled = base_policy(false);
        assert!(!compiled.fallback.allow_dacl_mutation);
        assert_eq!(compiled.capabilities, ["registryRead"]);
        assert_eq!(
            compiled.network_egress.unwrap().default,
            NetworkAction::Deny
        );
    }

    #[test]
    fn policy_hash_is_required_and_the_file_stays_pinned() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("policy.json");
        let bytes = br#"{"version":1,"filesystem":{":root":"read"},"network":false}"#;
        std::fs::write(&path, bytes).unwrap();
        let mut input = Invocation {
            policy_path: path.to_str().unwrap().into(),
            digest: format!("{:x}", Sha256::digest(bytes)),
            cwd: directory.path().to_str().unwrap().into(),
            command: vec!["cmd.exe".into()],
        };
        let (policy, pin) = read_policy(&input).unwrap();
        assert_eq!(policy.version, 1);
        assert!(OpenOptions::new().write(true).open(&path).is_err());
        drop(pin);
        input.digest = "incorrect".into();
        assert!(read_policy(&input).is_err());
    }

    #[test]
    fn unsupported_policy_version_never_reaches_native_creation() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("policy.json");
        let bytes = br#"{"version":2,"filesystem":{":root":"read"},"network":false}"#;
        std::fs::write(&path, bytes).unwrap();
        let input = Invocation {
            policy_path: path.to_str().unwrap().into(),
            digest: format!("{:x}", Sha256::digest(bytes)),
            cwd: directory.path().to_str().unwrap().into(),
            command: vec!["cmd.exe".into()],
        };
        assert!(read_policy(&input).is_err());
    }

    #[test]
    fn psec_hresult_is_reported_without_printing_sdk_text() {
        let response = ScriptResponse {
            error_message: "failed to create the process security environment: CreateProcessSecurityEnvironment failed (HRESULT = 0x80070057)".into(),
            ..Default::default()
        };
        assert_eq!(
            startup_detail(&response),
            ("CreateProcessSecurityEnvironment", None, Some(0x80070057))
        );
        let unrelated = ScriptResponse {
            error_message: "private argv 0x80070005".into(),
            ..Default::default()
        };
        assert_eq!(startup_detail(&unrelated), ("not-reported", None, None));
    }
}
