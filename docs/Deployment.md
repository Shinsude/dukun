# Deployment Guide

## Build

Build is declared via the standard packaging manifest with a setuptools backend and a minimum interpreter version. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only for the alternative configuration format. Build artifacts include distribution metadata and entry point declarations for the interactive console and the headless runner.

## Testing

The test runner configuration points to a dedicated test directory and adjusts the module search path to include the source directory. The offline suite exercises the orchestration loop, context truncation, configuration validation, the read-before-edit contract, memory capping, instruction loading, and the streaming parser. Interactive layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval classification, abort handling, and prompt forwarding. Live probes exercise end-to-end paths with valid credentials and network access.

## Environment Promotion

Configuration documents are kept alongside example files that illustrate the minimal required fields. Environment-specific values such as endpoint addresses and credential lookup names are supplied via configuration and resolved at runtime. Relative log paths are anchored to the project root, allowing the same configuration to be used from any working directory. Task documents may override the evaluator test command on a per-task basis.

## Operational Runbooks

### Monitoring

The system writes one structured record per event to an append-only file. Records include a timestamp, event name, and payload with task identifier, step, tool name, and result status. Monitoring consists of tailing this file and aggregating pass rates, step counts, tool error rates, and token usage.

### Backup

Persistent state to preserve includes the workspace directory, the user-wide settings document, the restricted credentials store, and the session transcript directory. Each of these is a regular file or directory in the user home or workspace. Backup is performed via standard file copy. No additional services require backup.

### Recovery

Recovery from corrupted user settings or credentials is automatic, treating the file as empty and continuing with defaults. Recovery from a corrupted session transcript skips the unreadable file and continues to list the remaining transcripts. Recovery from a sandbox failure is performed by the orchestrator, which ensures that the evaluator still produces a verdict and that cleanup is attempted even after errors.
