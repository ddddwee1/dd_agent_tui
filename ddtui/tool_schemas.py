"""JSON tool schemas exposed to the LLM."""

from __future__ import annotations


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a bash command and return stdout/stderr/exit code. "
                "The command runs in the project working directory by default. "
                "Output is truncated at 10 000 chars by default (override "
                "with max_output_chars). "
                "Dangerous patterns (sudo, curl, wget, chmod 777, mkfs, dd, rm -rf /) are blocked."
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
            "name": "bash_start",
            "description": (
                "Start a raw background shell command and return "
                "immediately with a job id (e.g. 'bg-3'). Use this only "
                "for simple background shell processes where you do NOT "
                "need structured task lifecycle, completion notifications, "
                "or agent-visible done signals. For long-running validation, "
                "build, test, download, training, profiling, or any operation "
                "whose completion matters, prefer task_start. stdout+stderr "
                "are merged into a log file. Same dangerous-pattern blacklist "
                "as bash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to run.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional working directory. Defaults to "
                            "the project directory."
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
                "and output path. STRICT WAITING RULE: for "
                "notify_on_complete=true tasks, do not call task_wait, "
                "repeated task_check, or repeated task_read merely because "
                "you are waiting for the task. Continue other useful work; "
                "if no useful work remains, update checkpoint_tool if helpful, "
                "then pause/finish the current response and let the runtime "
                "wake you when the notification arrives. Calling task_wait or "
                "polling task_check/task_read after finishing other work is "
                "the wrong behavior unless the user explicitly requested "
                "blocking or you need one short bounded inspection because "
                "the current output changes your next action. Use task_wait "
                "normally for notify_on_complete=false tasks. "
                "Use task_start for long-running work whose completion matters. "
                "Same dangerous-pattern blacklist as bash."
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
                            "and let the runtime wake it instead of blocking "
                            "with task_wait."
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
                "matters; use task_start for those. Same dangerous-pattern "
                "blacklist as bash."
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
                        "description": "How many trailing output lines to return. Default 80.",
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
                "BLOCK until a managed task finishes or timeout seconds "
                "elapse. The task keeps running on timeout. Use this for "
                "tasks started with notify_on_complete=false, for an explicit "
                "user request to block, or for one short bounded wait when an "
                "almost-finished result is immediately useful. STRICT RULE: "
                "if notify_on_complete=true, do not call task_wait merely "
                "because there is no other work left. Finish/pause the "
                "current response and wait for the completion notification "
                "instead. Repeated task_wait calls for a notified task are "
                "wrong unless the user explicitly asked for blocking."
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
                        "description": "Trailing output lines to return. Default 80.",
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
            "name": "bash_check",
            "description": (
                "Peek at a background job: returns current status "
                "(running / exited <code>) and the last N lines of "
                "merged stdout+stderr. Non-blocking — returns immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start (e.g. 'bg-3').",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "How many trailing lines of log to return. Default 50.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the default 10 000-char output cap "
                            "for the returned log tail. Hard upper bound "
                            "is 100 000."
                        ),
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_wait",
            "description": (
                "BLOCK until a background job finishes or *timeout* "
                "seconds elapse. Returns final status + log tail. Use "
                "this when you have nothing else to do but wait for a "
                "long task to finish. Default timeout 60s; max 600s. "
                "If the timeout fires while the job is still running, "
                "the job KEEPS RUNNING — call bash_wait again or "
                "bash_check to follow up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to block. Default 60, max 600.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Trailing log lines to return. Default 50.",
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": (
                            "Override the default 10 000-char output cap "
                            "for the returned log tail. Hard upper bound "
                            "is 100 000."
                        ),
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_kill",
            "description": (
                "Terminate a running background job. Sends SIGTERM by "
                "default (graceful); set force=true to send SIGKILL. "
                "No-op if the job has already exited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Send SIGKILL instead of SIGTERM. Default false.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_list",
            "description": (
                "List all known background jobs (running and recently "
                "finished) with id, command, status, runtime, and log "
                "path. Finished jobs are kept around for 5 minutes."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
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
                "Returns up to 2000 lines by default."
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
                        "description": "Line number to start reading from (0-indexed). Default 0.",
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
            "name": "write_file",
            "description": (
                "Create a new file or intentionally overwrite a whole file. "
                "For small or localized changes to an existing file, prefer "
                "apply_patch, edit_file, edit_lines, or multi_edit so the edit "
                "is incremental and reviewable. If force=False and the file "
                "already exists, the operation is refused."
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
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite without confirmation. Default true.",
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
                "Preferred tool for a small change in an existing file. Replace "
                "ONE occurrence of old_string with new_string and return a diff. "
                "Read or search the file first, then include enough surrounding "
                "context in old_string for an exact unique match, including "
                "whitespace and newlines. If there are multiple matches, either "
                "provide more context or specify occurrence (1-indexed). When "
                "multiple matches are found and no occurrence is given, the tool "
                "returns line numbers and surrounding context for each match."
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
                        "description": "Exact text to replace.",
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
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Codex-friendly alias for incremental file edits. This is the "
                "same editing capability as edit_file/multi_edit, not a unified "
                "diff parser. Use path + old_string + new_string for one exact "
                "replacement, or path + edits for multiple ordered replacements "
                "in one file. Prefer this over write_file when modifying an "
                "existing file."
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
                        "description": "Exact text to replace for a single edit.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text for a single edit.",
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": (
                            "Which occurrence to replace (1-indexed). "
                            "Only needed when old_string appears more than once."
                        ),
                    },
                    "edits": {
                        "type": "array",
                        "description": (
                            "Optional ordered list of edit operations, using the "
                            "same shape as multi_edit. Use this for several "
                            "changes in the same file."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {
                                    "type": "string",
                                    "description": "Exact text to replace.",
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
                "required": ["path"],
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
                                    "description": "Exact text to replace.",
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
                        "description": "Optional glob pattern for files (e.g. '*.py').",
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
                "matching by default. Results are truncated at 500 matches."
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
                        "description": "Optional glob to filter files (e.g. '*.py', '*.txt').",
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
                "single-level file_filter can't express. '**' matches any "
                "number of directories. Hidden files / directories (starting "
                "with '.') are skipped unless the pattern names them "
                "explicitly. Returns matching paths sorted by modification "
                "time descending (most-recent first), capped at 1000 results."
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
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Override the 10 000-char output cap. "
                            "Hard upper bound is 50 000."
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
                "status hint; the actual answer must be fetched with "
                "await_agent(session_id). The session keeps full "
                "memory (messages + reasoning) across rounds, so for "
                "follow-ups use chat_agent, not another spawn_agent. "
                "To run subagents in parallel, emit multiple "
                "spawn_agent calls (in one tool-call batch is fine), "
                "then await_agent each. Subagents have their own bash "
                "job table and CANNOT nest subagents. Token usage is "
                "billed here. Hit the live-session cap → end_agent an "
                "old one first."
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
                "background. Use await_agent(session_id) to fetch the "
                "answer. The subagent sees its full prior history "
                "including its own reasoning, so refer to earlier "
                "findings naturally. Errors if the session is still "
                "running an earlier round (await it first), if the "
                "previous result hasn't been await_agent'd yet, or if "
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
            "name": "await_agent",
            "description": (
                "Wait for a subagent's most recent round to finish and "
                "return its answer. Blocks for up to `timeout` seconds; "
                "if the subagent is still running when the timeout "
                "expires, returns a 'still running' notice and you can "
                "call await_agent again. Pass timeout=0 to poll without "
                "blocking. Once consumed, the answer is gone — call "
                "chat_agent to ask another question, then await_agent "
                "again. Errors if the session has no pending result "
                "(e.g. you await_agent'd twice in a row without a "
                "chat_agent in between)."
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
                "round, kill its background bash jobs, drop its "
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
            "name": "todo_tool",
            "description": (
                "Maintain a structured TODO list across the conversation. "
                "Pass the FULL list every call (overwrite mode) — there is "
                "no append/edit operation. Use it for any multi-step task "
                "so the user can see progress: list everything as 'pending' "
                "first, mark exactly one item 'in_progress' while you work "
                "on it, mark it 'completed' as soon as it's done, then "
                "promote the next 'pending' to 'in_progress'."
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
