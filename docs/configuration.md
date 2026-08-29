# Configuration

Configuration is supplied as a structured document. The loader accepts the primary format natively and the alternative format when the optional parsing library is available. Loading merges the supplied document deeply into a set of defaults, validates required sections, and validates enumerated values for approval mode and reasoning effort.

Defaults provide a base system prompt, maximum steps, language model settings, sandbox selection, tool list, evaluator command, logging sink, approval mode, context limits, automatic summarization threshold, verbosity, and skill routing preferences.

The language model section is forwarded to the registry, which maps the provider name to a concrete client class and validates that required constructor parameters are present. The sandbox, evaluator, and logger sections are handled similarly, with only the relevant keys forwarded. Tool construction shares a single edit ledger across file tools.

User-wide endpoint and model selections are kept in a separate hand-editable document. It enumerates endpoints with base address, credential lookup name, known models, and an optional note, plus the active endpoint, model, and reasoning effort. A parallel credentials store holds secret values with restricted permissions and is never written to the main settings file. Workflow definitions are kept in another document that stores named sequences of prompts.

Session transcripts are kept as one file per session under a dedicated directory, with an override location available via an environment variable. Each transcript records version, name, timestamp, workspace, model, summary, totals, goals, and the full message list.
