# Setup Guide

## Prerequisites

A supported interpreter version is required as declared in the project manifest. Network access to a compatible language model service is required for live operation. For isolated execution, a container runtime must be available on the host and its command-line interface must be executable.

## Installation

The project is installed via the standard packaging mechanism using the manifest in the repository root. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only when using the alternative configuration format.

After installation, two entry points are available: the interactive console and the headless runner. The interactive console can also be invoked directly from the source tree without installation.

## Configuration

Configuration is supplied as a structured document. The primary format is supported natively; the alternative format requires the optional parsing library. The loader merges the supplied document deeply into a set of defaults, so that partial documents inherit sensible values for omitted sections. Required sections are validated, and at least one tool must be listed. Approval mode and reasoning effort are validated against enumerated values.

Secrets are not stored in configuration files. The language model section names a lookup key that is resolved at runtime first from the process environment and then from a restricted credentials store. The store is created with owner-only permissions where the platform supports it.

User-wide endpoint and model selections are kept in a separate hand-editable document in the user home directory. An override location can be supplied via an environment variable for testing. The same mechanism allows redirecting the credentials store and the session transcript directory.

## Local Development

A workspace directory holds the repository under test. If the directory lacks version control initialization, an empty repository is initialized automatically. The workspace is reused across turns in interactive mode, and its location can be overridden at launch. Repository-specific instructions are discovered by searching the workspace root for well-known filenames, and the first match is used.

Durable per-workspace memory is maintained under a hidden directory inside the workspace and is capped to prevent unbounded growth. The system prompt is assembled from the base instruction, environment facts, known-failure knowledge, durable memory tail, and any repository instructions. It is rebuilt for each turn rather than frozen at startup, because the standing goal and any attached skills are set and cleared while the session is running. The environment facts are gathered the first time the prompt is needed, not during construction, so startup does not wait on version-control probes.

The interactive console requires a terminal for full functionality. When standard input or output is not a terminal, the console falls back to plain line input and omits decorative framing. A non-interactive single-message mode is available for scripting.

## Verification

The test suite is discovered in the dedicated test directory with the module search path adjusted to include the source directory. Running the suite exercises offline paths without network access or credentials. Live probes are available for end-to-end verification with valid credentials.
