# Implementations

## Language Model Clients

One implementation speaks the standard chat completions protocol over standard library networking facilities. It supports both buffered and streaming modes, accumulates tool-call deltas by index, handles server-sent event framing, and retries with backoff. It performs per-field downgrades when a server rejects optional parameters such as usage inclusion, reasoning effort, or token limit naming. Credentials are resolved from the configured lookup and included as a bearer token when present. A second implementation replays a fixed script of responses for offline testing and raises when the script is exhausted.

## Sandboxes

The host-directory sandbox executes directly in a workspace directory on the host. It creates the directory if needed, validates repository addresses and commit identifiers, and runs commands via the host process facility with polling to support abort and timeout. Path resolution rejects escapes via symlink resolution.

The container-based sandbox manages a disposable container via the container runtime command-line interface. It creates the container with resource limits, optionally disables networking, and executes commands via the runtime. File transfer is performed by staging locally and copying into the container to avoid shell quoting issues. Cleanup removes the container.

## Tools

File tools provide reading with truncation and ledger recording, writing with parent directory creation, editing with read-before-edit and stale-content checks, and directory listing with separate handling for sandboxes without a direct file view. The ledger enforces the read-before-edit invariant via content hashing.

Command tools execute a shell command and format the result as exit status, timeout flag, and truncated output, or display version control differences and discard changes.

Search tools walk the workspace without descending into ignored directories, skip files above a size limit or with non-text extensions, and return matching lines or file names with limits on result count. Web fetching retrieves a URL, enforces size and decompression limits, decodes according to declared character set, extracts visible text from markup while dropping non-visible elements, and blocks private or metadata hosts, returning an error string rather than raising.

## Loggers

The structured logger appends one serialized record per line with a timestamp, event name, and payload, using a lock for thread safety and suppressing input/output errors.
