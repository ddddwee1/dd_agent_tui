"""JSON tool schemas exposed to the LLM."""

from __future__ import annotations


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a synchronous bash command and return stdout/stderr/"
                "exit code. Times out (errors) at 30 s, and blocks "
                "everything else while it runs — for anything expected to "
                "take more than ~30 s, or whose completion matters (test "
                "suites, builds, downloads, training), use task_start "
                "instead and keep working while it runs. "
                "The command runs in the project working directory by "
                "default. Output is truncated at 10 000 chars by default "
                "(override with max_output_chars). "
                "Runs in a trusted local environment: only a few catastrophic "
                "footguns (rm -rf /, mkfs, dd of=/dev/*) are refused; ordinary "
                "tools like curl/wget/sudo are allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional working directory for this command. "
                            "Defaults to the project directory."
                        ),
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the default 10 000-char output cap. "
                            "Hard upper bound is 100 000."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_start",
            "description": (
                "Start a managed asynchronous task. The command runs in the "
                "background and streams stdout/stderr to a temporary output "
                "file. The agent can inspect progress with task_check or "
                "task_read while continuing other work. If notify_on_complete "
                "is true, the runtime will deliver a completion notification "
                "when the task exits, including status, return code, duration, "
                "and output path. Set notice_time to receive running notices "
                "while the task is still in progress. STRICT WAITING RULE: for "
                "notify_on_complete=true tasks, do not call task_wait, "
                "repeated task_check, or repeated task_read merely because "
                "you are waiting for the task. Continue other useful work; "
                "if no useful work remains, update checkpoint_tool if helpful, "
                "then pause/finish the current response and let the runtime "
                "wake you when the notification arrives. Subagents should call "
                "task_pause after task_start when no useful work remains. "
                "Calling task_wait or "
                "polling task_check/task_read after finishing other work is "
                "the wrong behavior unless the user explicitly requested "
                "blocking or you need one short bounded inspection because "
                "the current output changes your next action. "
                "Use task_start for long-running work whose completion matters. "
                "Same footgun guard as bash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run as a managed task.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional working directory. Defaults to the "
                            "project directory."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Short human-readable task name for task_list "
                            "and completion notifications."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional task description. Used as a fallback "
                            "name if name is omitted."
                        ),
                    },
                    "notify_on_complete": {
                        "type": "boolean",
                        "description": (
                            "Default true. If true, the runtime sends the "
                            "agent a completion notification when the task "
                            "exits; the agent can do other work or pause "
                            "and let the runtime wake it instead of blocking."
                        ),
                    },
                    "notice_time": {
                        "type": "number",
                        "description": (
                            "Seconds between running notices while the task "
                            "is still active. Default 60; 0 disables running "
                            "notices but still sends the completion "
                            "notification when notify_on_complete=true."
                        ),
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional local file paths to hash before launch. "
                            "The immutable SHA-256 snapshot is repeated in task "
                            "status and completion evidence. Maximum 8 files."
                        ),
                    },
                    "experiment_id": {
                        "type": "string",
                        "description": (
                            "Optional id from experiment_start. When set, "
                            "artifacts must include the experiment artifact at "
                            "the exact recorded SHA-256."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_start",
            "description": (
                "Start a persistent interactive PTY terminal and return a "
                "terminal_id (e.g. 'term-1'). Use for ssh, tmux attach, "
                "REPLs, debuggers, and installers where you need to keep "
                "prompt/session state and send multiple commands like a human "
                "using Terminal. For repeated remote work, prefer one "
                "terminal_start such as ssh/tmux and then terminal_send "
                "commands into it instead of repeatedly running ssh commands. "
                "Do not use this for non-interactive long jobs whose completion "
                "matters; use task_start for those. Same footgun guard "
                "as bash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Command to run under a PTY, e.g. "
                            "`ssh -tt host` or `ssh -tt host 'tmux new -A -s ddtui-agent'`."
                        ),
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional local working directory. Defaults to "
                            "the project directory."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Short display name for the terminal tab.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_send",
            "description": (
                "Type text into a persistent terminal PTY. Include '\\n' when "
                "you want to press Enter. Use this to send commands to an "
                "existing ssh/tmux/shell session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {
                        "type": "string",
                        "description": "Terminal id from terminal_start.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Exact text to write to the PTY.",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "description": (
                            "Optional small delay before draining output. "
                            "Default 200, max 5000."
                        ),
                    },
                },
                "required": ["terminal_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_read",
            "description": (
                "Read output from a persistent terminal log by byte offset. "
                "Use next_offset from the previous call to read only new "
                "output. For long non-interactive commands, avoid manual "
                "polling and use task_start instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {
                        "type": "string",
                        "description": "Terminal id from terminal_start.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Byte offset to start reading from. Default 0.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Maximum bytes/chars to read. Default 12 000, "
                            "hard max 100 000."
                        ),
                    },
                },
                "required": ["terminal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_interrupt",
            "description": "Send Ctrl-C to a running terminal PTY.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {
                        "type": "string",
                        "description": "Terminal id from terminal_start.",
                    },
                },
                "required": ["terminal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_close",
            "description": (
                "Close a persistent terminal and remove its tab. Sends "
                "SIGTERM then SIGKILL if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {
                        "type": "string",
                        "description": "Terminal id from terminal_start.",
                    },
                },
                "required": ["terminal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_list",
            "description": "List persistent terminal PTY sessions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_check",
            "description": (
                "Non-blocking status check for a managed task. Returns "
                "running/success/failed/killed, return code when available, "
                "output/status file paths, and the last N output lines. Use "
                "for one-off inspection when progress/output changes what "
                "you should do next. Do not repeatedly call task_check as a "
                "polling loop for notify_on_complete=true tasks; if nothing "
                "else is useful, pause/finish the current response and wait "
                "for the completion notification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id from task_start, e.g. 'task-3'.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "How many trailing output lines to return. Default 50.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the default returned text cap. Hard "
                            "upper bound is 100 000."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_read",
            "description": (
                "Incrementally read a managed task's output file by byte "
                "offset. Use next_offset from the previous call to continue "
                "without rereading old output. Use for one-off or genuinely "
                "incremental output inspection, not as an idle polling loop "
                "for notify_on_complete=true tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id from task_start.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Byte offset to start reading from. Default 0.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Maximum bytes/chars to read. Default 12 000, "
                            "hard max 100 000."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_wait",
            "description": (
                "Deprecated compatibility stub. This tool no longer blocks "
                "and is not advertised to agents. Use task_check/task_read "
                "for inspection, task_start notifications for parent wakeup, "
                "and task_pause for subagent waiting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id from task_start.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to block. Default 60, max 600.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Trailing output lines to return. Default 50.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the default returned text cap. Hard "
                            "upper bound is 100 000."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_pause",
            "description": (
                "Subagent-only: park this subagent round while waiting for "
                "task_start notifications. Call after starting one or more "
                "notify_on_complete=true tasks when no useful work remains. "
                "This is fire-and-forget: it commits a tool result, enters "
                "the waiting phase, and the runtime will wake the subagent "
                "with [Async task notice] or [Async task complete]. Do not "
                "use task_wait."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Task ids to wait on. Defaults to all currently "
                            "running notified tasks in this subagent."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason shown in status/check output.",
                    },
                    "next_action": {
                        "type": "string",
                        "description": (
                            "What the subagent should do after the task event "
                            "wakes it."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_kill",
            "description": (
                "Terminate a running managed task. Sends SIGTERM by default; "
                "set force=true to send SIGKILL. Completion notification is "
                "still delivered for notify_on_complete tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id from task_start.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Send SIGKILL instead of SIGTERM. Default false.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": (
                "List managed tasks with id, name, status, runtime, command, "
                "and output path. By default includes recently finished tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_finished": {
                        "type": "boolean",
                        "description": "Default true. Set false to list only running tasks.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_add",
            "description": (
                "Add a reusable project-local note to <repo>/.ddtui/notes/notes.json "
                "or DDTUI_PROJECT_NOTES_DIR. Use this for durable facts such "
                "as verified commands, environment setup, repo-specific gotchas, "
                "API conventions, or user-approved decisions. Do not store "
                "secrets, one-off transient observations, or large raw logs. "
                "Prefer concise notes with tags. If the note is an inference "
                "rather than a verified fact, set source='inferred' and lower "
                "confidence. Before adding, prefer project_note_search and "
                "project_note_update when an existing related note should be "
                "consolidated instead of duplicated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short note title.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Concise reusable note body. Do not include secrets.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional tags such as runbook, test, deploy, "
                            "remote, debug, gotcha, architecture, style, "
                            "decision, env."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": "One of user, observed, inferred, imported. Default observed.",
                    },
                    "confidence": {
                        "type": "string",
                        "description": "One of low, medium, high. Default medium.",
                    },
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_search",
            "description": (
                "Search project-local notes for reusable context before "
                "guessing repo-specific commands, environment setup, gotchas, "
                "or conventions. Results are not automatically authoritative; "
                "check source/confidence and verify when stale or risky."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tag filter; all listed tags must match.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results. Default 5, max 50.",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived notes. Default false.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_list",
            "description": (
                "List recent project-local notes, optionally filtered by tags. "
                "Use this to inspect available runbook memory without a "
                "specific search query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tag filter; all listed tags must match.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum notes. Default 20, max 100.",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived notes. Default false.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_read",
            "description": "Read one full project-local note by note_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "string",
                        "description": "Note id from project_note_search/list.",
                    },
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_update",
            "description": (
                "Patch an existing project-local note in place. Use update "
                "when a note is stale, incomplete, or should be consolidated. "
                "Prefer updating an existing related note over adding a "
                "near-duplicate note. Unspecified fields are unchanged. "
                "Set archived=true to hide an outdated note from normal "
                "search/list results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "string",
                        "description": "Existing note id.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional replacement title.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional replacement body. Do not include secrets.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional replacement tag list.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional source: user, observed, inferred, imported.",
                    },
                    "confidence": {
                        "type": "string",
                        "description": "Optional confidence: low, medium, high.",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Set true to archive/soft-delete; false to restore.",
                    },
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_note_delete",
            "description": (
                "Soft-delete a project-local note by setting archived=true. "
                "Archived notes are hidden from normal search/list but can be "
                "included with include_archived=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "string",
                        "description": "Existing note id.",
                    },
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a text file, with optional offset/limit for pagination. "
                "Returns up to 2000 lines by default. Each output line is "
                "prefixed with its 1-indexed line number followed by a tab "
                "(cat -n style); these numbers match edit_lines and are NOT "
                "part of the file — never include them in edit_file/"
                "multi_edit old_string. Very long lines are clipped and very "
                "large results are capped with a hint for which offset to "
                "continue from."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the file. "
                            "Parameter name is 'path' (not 'file_path')."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Line number to start reading from (1-indexed, "
                            "matching edit_lines). Default 1 (first line)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to return. Default 2000.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": (
                "Read Markdown with progressive disclosure. With no heading/"
                "anchor, returns a compact outline plus resolved explicit "
                "links. With heading or anchor, returns only that section and "
                "a persistent document receipt. Prefer this over loading a "
                "whole long Markdown file. It does not choose which route or "
                "technical strategy is relevant; use follow_doc_link only for "
                "the explicit link selected by your current reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative Markdown file path.",
                    },
                    "heading": {
                        "type": "string",
                        "description": (
                            "Exact or uniquely matching heading text. Mutually "
                            "exclusive with anchor."
                        ),
                    },
                    "anchor": {
                        "type": "string",
                        "description": (
                            "Markdown heading anchor, with or without '#'. "
                            "Mutually exclusive with heading."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "follow_doc_link",
            "description": (
                "Follow one explicit Markdown link from a prior read_doc "
                "receipt. Resolves the relative path and heading anchor, reads "
                "only that target section (or its outline when no anchor was "
                "present), and records the edge. Never crawls links "
                "automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "receipt_id": {
                        "type": "string",
                        "description": "Document receipt id returned by read_doc.",
                    },
                    "link_id": {
                        "type": "string",
                        "description": "Exact link id such as link-1.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Unique substring of the displayed link label.",
                    },
                },
                "required": ["receipt_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "doc_route_status",
            "description": (
                "Show persistent Markdown route receipts, followed links, and "
                "broken explicit targets. Use after compaction/resume or before "
                "claiming that a selected documentation route was covered. "
                "Unfollowed links are not automatically mandatory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "receipt_id": {
                        "type": "string",
                        "description": "Optional receipt to show; default all.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or intentionally overwrite a whole file. "
                "Overwriting an EXISTING file is refused unless you have "
                "read it with read_file in this session and it has not "
                "changed on disk since — read first, then overwrite. For "
                "small or localized changes prefer edit_file or multi_edit "
                "so the edit is incremental and reviewable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the file. "
                            "Parameter name is 'path' (not 'file_path')."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Preferred tool for exact-match edits in an existing file. "
                "Replace ONE occurrence of old_string with new_string (or "
                "every occurrence with replace_all=true) and return a diff. "
                "Read or search the file first, then include enough "
                "surrounding context in old_string for an exact unique "
                "match, including whitespace and newlines. When old_string "
                "matches more than once and neither occurrence nor "
                "replace_all is given, the tool returns line numbers and "
                "surrounding context for each match so you can refine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the file. "
                            "Parameter name is 'path' (not 'file_path')."
                        ),
                    },
                    "old_string": {
                        "type": "string",
                        "description": (
                            "Exact text to replace, as it appears in the "
                            "file — do NOT include the line-number prefix "
                            "shown by read_file."
                        ),
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": (
                            "Which occurrence to replace (1-indexed). "
                            "Only needed when old_string appears more than once."
                        ),
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "Replace every occurrence. Default false. "
                            "Mutually exclusive with occurrence."
                        ),
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_lines",
            "description": (
                "Edit an existing file by line numbers (1-indexed) and return a "
                "diff. Use this for precise line-level edits when you already "
                "know which lines to change. "
                "mode='replace' replaces lines [start_line..end_line] inclusive with content. "
                "mode='insert' inserts content BEFORE start_line; end_line is ignored. "
                "mode='delete' removes lines [start_line..end_line]; content is ignored. "
                "content can be multi-line (use '\\n'). For substring substitution within a line, "
                "use edit_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the file. "
                            "Parameter name is 'path' (not 'file_path')."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "Edit mode.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-indexed).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line (1-indexed, inclusive). Required for replace/delete.",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content. Required for replace/insert.",
                    },
                },
                "required": ["path", "mode", "start_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Preferred tool for several related changes in the same existing "
                "file. Apply multiple string replacements to ONE file in a "
                "single call and return one diff. "
                "Edits are applied in order; each one operates on the result of "
                "the previous, so a later edit can reference text introduced by "
                "an earlier one. All edits must succeed atomically — if any one "
                "fails, the file is left untouched. Use this instead of multiple "
                "edit_file calls when changing several parts of the same file. "
                "Each edit follows edit_file rules: old_string must match exactly. "
                "If old_string appears more than once in the (current) buffer, "
                "either set replace_all=true or specify occurrence (1-indexed). "
                "replace_all and occurrence are mutually exclusive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the file. "
                            "Parameter name is 'path' (not 'file_path')."
                        ),
                    },
                    "edits": {
                        "type": "array",
                        "description": "Ordered list of edit operations.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {
                                    "type": "string",
                                    "description": (
                                        "Exact text to replace (without "
                                        "read_file's line-number prefix)."
                                    ),
                                },
                                "new_string": {
                                    "type": "string",
                                    "description": "Replacement text.",
                                },
                                "occurrence": {
                                    "type": "integer",
                                    "description": (
                                        "Which occurrence to replace (1-indexed). "
                                        "Required when old_string matches multiple "
                                        "times and replace_all is not set."
                                    ),
                                },
                                "replace_all": {
                                    "type": "boolean",
                                    "description": (
                                        "Replace every occurrence. Default false. "
                                        "Mutually exclusive with occurrence."
                                    ),
                                },
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories under a given path, recursively up to a "
                "specified depth (default 2). Supports an optional glob filter for "
                "file names (e.g. '*.py'). Directories are suffixed with '/'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory path to list. Default '.'. "
                            "Parameter name is 'path' (not 'file_path' or 'directory')."
                        ),
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum recursion depth. Default 2.",
                    },
                    "file_filter": {
                        "type": "string",
                        "description": (
                            "Optional glob pattern. Without '/' it matches "
                            "file names at any depth (e.g. '*.py'); with "
                            "'/' it matches the relative path ('**' spans "
                            "directories, '*' does not)."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": (
                "Search for a regex pattern in files under a path. "
                "Returns matching lines with file:line:content. "
                "Supports file_filter glob (e.g. '*.py') and case-insensitive "
                "matching by default. Results are truncated at 500 matches. "
                "Fast when ripgrep-backed; .gitignore'd files are included "
                "by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory or file to search in. Default '.'. "
                            "Parameter name is 'path' (not 'file_path' or 'directory')."
                        ),
                    },
                    "file_filter": {
                        "type": "string",
                        "description": (
                            "Optional glob to filter files. Without '/' it "
                            "matches file names at any depth (e.g. '*.py'); "
                            "with '/' it matches the relative path ('**' "
                            "spans directories, '*' does not)."
                        ),
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Set to true for case-sensitive search. Default false.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find files by glob pattern, recursively. Use this when you "
                "need a multi-level pattern like '**/*.py' that list_files's "
                "single-level file_filter can't express. Semantics: a "
                "pattern without '/' matches file names at any depth "
                "('*.py' finds every Python file); in a pattern with '/', "
                "'*' stays within one path component and '**' spans "
                "directories ('**/*.py' includes top-level files, "
                "'src/*.py' does NOT match src/a/b.py). Returns matching "
                "paths sorted by modification time descending (most-recent "
                "first), capped at 1000 results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern (e.g. '**/*.py', 'src/**/test_*.ts')."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Root directory. Default '.'. "
                            "Parameter name is 'path' (not 'file_path' or 'directory')."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch the contents of an http(s) URL. HTML pages are stripped to "
                "plain text (script/style/svg removed, whitespace collapsed); JSON, "
                "XML, plain text and similar are returned verbatim. Binary content "
                "(images, video, pdf, octet-stream, …) is refused. Output is "
                "truncated at 10 000 chars by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the 10 000-char output cap. "
                            "Hard upper bound is 50 000. (Alias: max_chars.)"
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web via Brave Search and return a ranked "
                "list of results (title / URL / snippet). Use this to "
                "find authoritative URLs to feed into web_fetch, or "
                "to get a quick survey of recent information not in "
                "the model's training. Returns plain text, one block "
                "per result. Default 5 results, max 20."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — same syntax as a Brave search box.",
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Number of results to return. Default 5, "
                            "max 20."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Create a persistent subagent session, send it the "
                "first prompt, and IMMEDIATELY return — the subagent "
                "runs in the background. Returns a session_id and a "
                "status hint. Keep working / end your response; an unread "
                "result is auto-delivered a moment later as a "
                "'[Subagent result]' notification (same pipeline as task "
                "completions). Use agent_check(session_id) only for a "
                "non-blocking status/output inspection. The session keeps full memory "
                "(messages + reasoning) across rounds, so for "
                "follow-ups use chat_agent, not another spawn_agent. "
                "To run subagents in parallel, emit multiple "
                "spawn_agent calls (in one tool-call batch is fine). "
                "Subagents have their own bash job table and CANNOT "
                "nest subagents. Token usage is billed here. Hit the "
                "live-session cap → end_agent an old one first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The subagent's first user message. State the "
                            "task, success criteria, and any context "
                            "(file paths, prior findings) — the subagent "
                            "starts with no memory of this conversation."
                        ),
                    },
                    "system": {
                        "type": "string",
                        "description": (
                            "Optional extra system instruction appended "
                            "to the framework system prompt. Use to "
                            "narrow the subagent's role, e.g. 'You are "
                            "a read-only investigator: report findings, "
                            "do not modify files.' Persists for the "
                            "lifetime of the session."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model id override for this "
                            "subagent — must be a model of the CURRENT "
                            "provider. Defaults to the parent's model. "
                            "Consider a cheaper/faster model for "
                            "search or read-only investigation tasks."
                        ),
                    },
                    "effort": {
                        "type": "string",
                        "description": (
                            "Optional reasoning-effort override for "
                            "this subagent. Defaults to the parent's "
                            "current setting."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_agent",
            "description": (
                "Send another message to an existing subagent session "
                "and IMMEDIATELY return — the round runs in the "
                "background. The result auto-delivers when ready; use "
                "agent_check(session_id) only for a non-blocking status/"
                "output inspection. The subagent sees its full prior history "
                "including its own reasoning, so refer to earlier "
                "findings naturally. Errors if the session is still "
                "running or waiting on an earlier round, if the "
                "previous result hasn't been agent_check'd yet, or if "
                "the session_id is unknown."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The id returned by spawn_agent (e.g. 'sub-1').",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Next user message to the subagent.",
                    },
                },
                "required": ["session_id", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agent_check",
            "description": (
                "Non-blocking status/output check for a subagent session. "
                "Returns running/waiting/idle status immediately. If a final "
                "answer is ready, returns and consumes it so chat_agent can "
                "send a follow-up. Does not wait; if the answer is not ready, "
                "keep working or let the '[Subagent result]' notification "
                "arrive automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The id returned by spawn_agent.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "await_agent",
            "description": (
                "Deprecated compatibility stub. This tool no longer blocks "
                "and is not advertised to agents. Use agent_check for "
                "non-blocking subagent status/output checks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The id returned by spawn_agent.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Max seconds to wait for the round to "
                            "finish. Default 60, max 600, 0 = poll. "
                            "If the round is fast, returns as soon as "
                            "the answer is ready (well before timeout)."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_agent",
            "description": (
                "Release a subagent session: cancel any in-flight "
                "round, kill its background tasks and terminals, drop its "
                "conversation. Call this as soon as you no longer need "
                "the session, to free a slot and stop the idle timer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The id returned by spawn_agent.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compact_self",
            "description": (
                "SUBAGENT-ONLY. Compress your own conversation history: "
                "summarize earlier messages into a single system note, "
                "keeping the two most recent user/assistant turns "
                "verbatim. Use when the parent agent's chat_agent prompt "
                "tells you your context is filling up, or when you "
                "judge your own history is too long for the work left "
                "to do. Takes no arguments. Returns before/after "
                "message + character counts. The parent agent cannot "
                "call this — it has its own /compact slash command."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_start",
            "description": (
                "Start a temporary exploration span in the parent "
                "conversation. Use when the next work is evidence "
                "gathering, probing, comparison, or low-signal search, "
                "and the raw intermediate context should not live in "
                "the main transcript after a concise conclusion is "
                "produced. Good uses include single-feature probing, "
                "bug debugging, hypothesis checks, code archaeology, "
                "design scouting, broad web/docs research with low "
                "information density, API/library behavior probes, "
                "environment or permission checks, performance/numeric "
                "experiments, test discovery, data sample inspection, "
                "log clustering, and read-only risk audits. Do NOT use "
                "for final implementation, final user answers, "
                "irreversible actions, or durable project knowledge "
                "that belongs in files, project notes, or checkpoint_tool. "
                "Call explore_start ALONE in its own tool-call batch; "
                "after the exploration, call explore_end ALONE to archive "
                "the raw span and replace it with a summary, or "
                "explore_cancel if the span should remain normal history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "The concrete question this exploration should answer."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "debug",
                            "feature_probe",
                            "code_archaeology",
                            "design_scouting",
                            "web_research",
                            "env_probe",
                            "perf_experiment",
                            "data_inspection",
                            "test_discovery",
                            "log_clustering",
                            "risk_audit",
                            "hypothesis_check",
                            "api_probe",
                            "custom",
                        ],
                        "description": (
                            "Exploration category. Default feature_probe."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why this should be explored in a temporary span "
                            "instead of the main line."
                        ),
                    },
                    "expected_outputs": {
                        "type": "string",
                        "description": (
                            "What evidence or conclusion should come back "
                            "to the main task."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_end",
            "description": (
                "End the active exploration span. The app archives the raw "
                "messages between explore_start and this call, summarizes "
                "them into a system exploration summary, and removes the "
                "raw span from the live model context. Call this ALONE in "
                "its own tool-call batch once the exploration has a useful "
                "conclusion or an explicit uncertainty to carry forward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome_hint": {
                        "type": "string",
                        "description": (
                            "Optional concise statement of the conclusion, "
                            "negative result, or remaining uncertainty. "
                            "The summarizer uses this as guidance but still "
                            "checks the raw exploration history."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_cancel",
            "description": (
                "Cancel the active exploration without summarizing or "
                "rewriting history. Use when the span was opened by "
                "mistake, the raw work is actually needed verbatim, or "
                "the exploration should be treated as normal conversation. "
                "Call this ALONE in its own tool-call batch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Optional short reason for cancelling.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_tool",
            "description": (
                "Maintain a concise working-state checkpoint for the current "
                "conversation. This replaces the previous checkpoint; do not "
                "append a diary. Use during long or multi-branch tasks to "
                "record the current goal, focus, hypotheses, evidence, "
                "decisions, blockers, active task/subagent refs, touched files, "
                "and next steps. Use todo_tool for checklist items. Use "
                "project_note_* only for durable repo knowledge. Keep it short "
                "and factual."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Current overall objective.",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "planning",
                            "in_progress",
                            "waiting",
                            "blocked",
                            "ready_for_review",
                            "done",
                            "abandoned",
                        ],
                        "description": "Current work state.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short factual summary of current state.",
                    },
                    "current_focus": {
                        "type": "string",
                        "description": "What you are focused on right now.",
                    },
                    "hypotheses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Current hypotheses or debugging theories.",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Verified evidence gathered so far.",
                    },
                    "decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Important decisions already made.",
                    },
                    "blockers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Known blockers or open constraints.",
                    },
                    "next_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Immediate next steps from here.",
                    },
                    "active_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Active task ids, subagent ids, PR ids, etc.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant touched or inspected file paths.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Confidence in this checkpoint. Default medium.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short caveat or handoff note.",
                    },
                },
                "required": ["goal", "status", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_get",
            "description": (
                "Read the current conversation checkpoint. Use when resuming, "
                "after long context, after task notifications, or whenever "
                "you are unsure of the current working state."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_clear",
            "description": (
                "Clear the current conversation checkpoint and remove the "
                "checkpoint sidebar block. Use when the tracked work, waiting "
                "state, blocker, or handoff is resolved. Task completion "
                "notifications may also auto-clear waiting checkpoints whose "
                "active_refs only contain completed task ids."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Optional short reason for clearing.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "experiment_start",
            "description": (
                "Start a bounded implementation experiment and snapshot one "
                "candidate artifact by SHA-256. Call after writing a candidate "
                "and before validation. Every changed artifact needs a new "
                "candidate or fix experiment. Budget overruns are recorded as "
                "warnings rather than silently forgotten."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_path": {
                        "type": "string",
                        "description": "Local candidate file to hash immutably.",
                    },
                    "hypothesis": {
                        "type": "string",
                        "description": "One falsifiable reason this attempt may help.",
                    },
                    "boundary": {
                        "type": "string",
                        "description": "Current failing or validation boundary.",
                    },
                    "attempt_type": {
                        "type": "string",
                        "enum": ["candidate", "fix"],
                        "description": "Default candidate. Fix requires parent_id.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "Parent experiment id for a same-boundary fix.",
                    },
                    "max_candidates": {
                        "type": "integer",
                        "description": "Declared candidate budget. Default 3.",
                    },
                    "max_same_boundary_fixes": {
                        "type": "integer",
                        "description": "Declared fix budget per boundary. Default 2.",
                    },
                },
                "required": ["artifact_path", "hypothesis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "experiment_record",
            "description": (
                "Record correctness, performance, source-gate, diagnostic, or "
                "other evidence against one immutable experiment SHA. Bind "
                "managed commands with task_id. Performance evidence is marked "
                "invalid until correctness has passed for the same SHA; task "
                "failure or artifact drift is also recorded explicitly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "correctness", "performance", "source_gate",
                            "diagnostic", "other",
                        ],
                    },
                    "result": {
                        "type": "string",
                        "enum": ["pass", "fail", "inconclusive", "skipped"],
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Completed task bound to this experiment.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command when no managed task was used.",
                    },
                    "evidence": {"type": "string"},
                    "metric_name": {"type": "string"},
                    "metric_value": {
                        "type": "string",
                        "description": (
                            "Metric value as text, including units when useful "
                            "(for example '13.2 us')."
                        ),
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["continue", "keep", "rollback", "stop"],
                    },
                },
                "required": ["experiment_id", "kind", "result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "experiment_status",
            "description": (
                "Show compact experiment budgets, immutable SHA-256 values, "
                "evidence validity, and keep/rollback state. Use before final "
                "claims and after compaction/resume."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Optional experiment to show; default all.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_tool",
            "description": (
                "Maintain a structured TODO list across the conversation. "
                "Pass the FULL list every call (overwrite mode) — there is "
                "no append/edit operation. Use it for any multi-step task "
                "so the user can see progress: list everything as 'pending' "
                "first, mark exactly one item 'in_progress' while you work "
                "on it, mark it 'completed' as soon as it's done, then "
                "promote the next 'pending' to 'in_progress'. Before "
                "delivering your final answer, reconcile the list: every "
                "finished item must be marked 'completed' (including the "
                "last one — a common miss); anything not done should be "
                "removed or explicitly explained, never left dangling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Full ordered list. Replaces any existing list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Task description (imperative verb).",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current state of this item.",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
]
