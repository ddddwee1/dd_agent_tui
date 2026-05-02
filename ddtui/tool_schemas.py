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
                "Start a long-running bash command in the BACKGROUND and "
                "return immediately with a job id (e.g. 'bg-3'). The "
                "process is detached (own session) so it survives short "
                "agent stalls. stdout+stderr are merged into a log file. "
                "Use bash_check / bash_wait / bash_kill / bash_list to "
                "interact with it. Cap: at most 5 jobs running at once. "
                "Same dangerous-pattern blacklist as bash."
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
                        "description": "Absolute or relative path to the file.",
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
                "Create or overwrite a file with the given content. "
                "If force=False and the file already exists, the operation is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
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
                "Replace ONE occurrence of old_string with new_string in a file. "
                "old_string must match exactly. If there are multiple matches, "
                "either provide a larger string with more surrounding context to "
                "make it unique, or specify the occurrence number (1-indexed). "
                "When multiple matches are found and no occurrence is given, "
                "the tool returns line numbers and surrounding context for each match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
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
            "name": "edit_lines",
            "description": (
                "Edit a file by line numbers (1-indexed). Use this for precise "
                "line-level edits when you know which lines to change. "
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
                        "description": "Absolute or relative path to the file.",
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
                "Apply multiple string replacements to ONE file in a single call. "
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
                        "description": "Absolute or relative path to the file.",
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
                        "description": "Directory path to list. Default '.'.",
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
                        "description": "Directory or file to search in. Default '.'.",
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
                        "description": "Root directory. Default '.'.",
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
