# Console

The console module provides the interactive terminal interface using only standard library facilities. It implements styling via terminal escape sequences, with a flag to disable styling for non-terminal output.

The console manages a single session that owns the workspace, sandbox, context manager, system prompt, tool set, language model client, approval policy, and event bus. It maintains totals for tokens, turns, and errors, recent tool history, and per-turn cache metrics. It also manages a streaming renderer that buffers fragments until line boundaries to apply formatting, a spinner that runs on a background thread while the model is working, and a line editor that provides inline completion.

The line editor reads single keys and provides completion for commands and workspace paths. It supports navigation keys, history, and a popup that can be dismissed or re-invoked. When standard input or output is not a terminal, it falls back to plain line input.

The startup card is rendered as a centered adaptive box whose width follows the terminal width with small margins. The card displays the product name, tagline, and version. It disappears after the first turn. The bottom prompt is fixed at the window bottom with a separator and a single-line status showing the model, reasoning effort, and workspace. The prompt uses a distinctive colour and includes a live token estimate during streaming.

The console expands file mentions, resolves globs, and attaches content with size limits. It also handles goal injection, skill attachment, and workflow launching, and auto-saves transcripts to a per-user directory for later resumption.

The help text enumerates all slash commands and describes file reference syntax. The console banner displays model, endpoint, workspace, version control status, approval mode, tool count, and instruction file information.
