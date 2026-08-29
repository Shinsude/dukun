# Overview

The platform is a modular harness that delegates software engineering tasks to a compatible language model. It provides a sandboxed workspace, a bounded set of file and command tools, and an automated grading step that executes a configured test command. Operation is available through an interactive terminal interface for iterative development and through a headless command interface for single-task execution.

The interactive console maintains a persistent workspace across turns, retains conversation history within configurable bounds, and supports inline references to workspace files. The headless runner executes one task from start to evaluation and records a structured event log.

For detailed information, see the guides in the documentation directory: Architecture Guide for design and layering, Setup Guide for installation and local development, API Reference for interfaces and commands, Deployment Guide for build and operational concerns, and the module-specific documents for per-domain details.

# Quick Start

Launch the interactive console and, if needed, configure the language model endpoint and credential. The console will prompt for any missing endpoint information on first launch. Once configured, type a natural language request; the system will explore the workspace, make changes, and run verification steps. Follow-up messages continue the same conversation and workspace.

For non-interactive use, provide a configuration document and a task document to the headless runner. The runner provisions the workspace, executes the agent loop, grades the result, and writes an event log. Exit status indicates whether the automated grading passed.

# Documentation Map

- Architecture Guide — high-level design, module interactions, and trade-offs.
- Setup Guide — prerequisites, installation, and local development.
- API Reference — public interfaces, configuration sections, and commands.
- Deployment Guide — build, testing, and operational runbooks.
- Module-specific docs — per-domain details for core, interfaces, implementations, and console.
