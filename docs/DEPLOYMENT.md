# Deployment Guide

## Build

Build is declared via the standard packaging manifest with a setuptools backend and a minimum interpreter version. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only for the alternative configuration format. Build artifacts include distribution metadata and entry point declarations for the interactive console and the headless runner. The manifest also declares test discovery and module search path adjustments for the test suite.

## Testing

The test runner configuration points to a dedicated test directory and adjusts the module search path to include the source directory. The offline suite exercises the orchestration loop including final-answer handling, step-limit termination, unknown-tool resilience, and error paths, context truncation with pinned messages and orphan avoidance, configuration validation including context limits, the read-before-edit contract including unread and stale-content rejection, memory capping including single-line truncation handling, instruction file discovery and preference order, the streaming parser including content accumulation, tool-call reassembly, and malformed-chunk handling, model discovery filtering and ranking, approval classification, session persistence with size caps, and file-tool safety checks. Interactive layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding. Live probes exercise end-to-end paths with valid credentials and network access and are not required for the offline suite. The full suite is expected to pass without network access aside from the live probes, and the container-based sandbox tests require a container runtime on the host.

## Environment Promotion

Configuration documents are kept alongside example files that illustrate the minimal required fields. Environment-specific values such as endpoint addresses and credential lookup names are supplied via configuration and resolved at runtime, with relative log paths anchored to the project root so the same configuration works from any working directory. Task documents may override the evaluator test command on a per-task basis. User-wide settings, credentials, and session transcripts are kept in the user home directory with override locations available via environment variables for testing, and all are written atomically via temporary files with restricted permissions.

## Operational Runbooks

### Monitoring

The system writes one structured record per event to an append-only file. Records include a timestamp, event name, and payload with task identifier, step, tool name, and result status including elapsed time and success flag. Monitoring consists of tailing this file and aggregating pass rates, step counts, tool error rates, and token usage including cache hit metrics. The console also maintains per-turn totals and displays them in the status area.

### Backup

Persistent state to preserve includes the workspace directory, the user-wide settings document, the restricted credentials store, the session transcript directory, and the workflow definitions file. Each of these is a regular file or directory in the user home or workspace. Backup is performed via standard file copy while the system is idle. No additional services require backup.

### Recovery

Recovery from corrupted user settings or credentials is automatic, treating the file as empty and continuing with defaults, with a quarantined copy of the corrupted file retained alongside the original. Recovery from a corrupted session transcript skips the unreadable file and continues to list the remaining transcripts. Recovery from a corrupted workflow file similarly returns an empty collection. Recovery from a sandbox failure is performed by the orchestrator, which ensures that the evaluator still produces a verdict and that cleanup is attempted even after errors, and that abort signals are propagated to running commands.

