# term-buddy

Term Buddy opens a tmux workspace with your Bash shell on the left and a local-AI
debugging companion on the right. It observes completed commands and their output,
comments when useful, answers questions, completes command lines, and can run bounded
diagnostic commands of its own.

It is designed for Linux servers: the runtime has no third-party Python packages,
model traffic goes to the endpoint you configure, and session data is kept in a
private directory under `$XDG_RUNTIME_DIR` or `/tmp/term-buddy-$UID`.

## Requirements

- Linux or another Unix-like system
- Python 3.10+
- tmux
- Bash
- An OpenAI-compatible chat-completions API (defaults to
  `http://127.0.0.1:8080/v1`)

On Debian or Ubuntu:

```bash
sudo apt install tmux python3
```

## Install

From a clone:

```bash
./install.sh
```

For development, no installation is needed:

```bash
./bin/term-buddy
```

## Quick start

Start the default session:

```bash
term-buddy
```

Use the left pane normally. Your existing tmux configuration and pane-navigation
bindings remain active. In the left shell, ask a direct question with:

```bash
buddy why did the previous command fail?
```

You can also move to the right pane and type questions there. Right-pane commands:

- `/run COMMAND` runs a diagnostic command.
- `/stop` stops the current Buddy task, clears queued questions, and discards late responses.
- `/learn` loads the current project into model context.
- `/clear` clears the Buddy pane.
- `/help` displays help.
- `/quit` closes only the Buddy process.

When the model needs more evidence, it can request `<tool>...</tool>` commands.
Term Buddy executes each through the same read-only policy (or yolo policy), shows
the results, and lets the model continue investigating without an arbitrary command
limit. If a requested tool is denied, the reason and allowed alternatives are sent
back to the model so it can recover instead of abandoning the task.
Term Buddy also recognizes explicitly labeled Markdown tool requests from models
that occasionally fail to emit the required `<tool>...</tool>` wrapper.

Autocomplete is off by default because it performs an inference while you edit. To
enable it, set `"autocomplete": true` in the configuration, restart the session,
then press **Shift-Tab** while editing a command. Term Buddy inserts the returned
suffix without pressing Enter.

The Buddy header shows its current model and endpoint, approximate context-token
usage, tool safety mode, autocomplete state, and an animated thinking indicator.
Model output streams into the pane as it is generated. A bottom activity panel shows
request scope, elapsed time, first-token latency, approximate output speed,
reasoning-token progress, working directory, exact tool activity, retries, errors,
and cancellation. Click `[details]` or press **F2** to collapse or expand it. Private
chain-of-thought text is not displayed.
The token count is a conservative source-code estimate and project snapshots retain
headroom for system instructions, questions, tool results, and generated output.

To load the current project into the session context, run this from the left shell:

```bash
buddy learn project
```

Loading makes the snapshot available but does not attach it to normal questions.
Explicitly opt into the large snapshot only when it is useful:

```bash
buddy /proj what should I change in ScrapForm.vue to make it green?
```

Normal questions remain lightweight, even after learning a project:

```bash
buddy how do I run this on port 3005?
buddy what is the latest Git commit?
```

Git and system-operational questions remain lightweight even if `/proj` is supplied,
unless `optimize_operational_project_questions` is disabled. Their authoritative
information comes from targeted live tools rather than the static project snapshot.

Term Buddy uses `rg --files` (and therefore normal ignore rules) to build a complete
file tree, then reads text files in useful order until the configured project-context
budget is full. Binary files, common secret files, private keys, VCS metadata,
dependency directories, and ignored files are not sent to the model. The loaded
project remains available for later questions in that Buddy session. Re-run the
command after substantial project changes.

The completion summary reports discovered and loaded files separately, files
deferred because the context budget filled, excluded dependency/build/VCS
directories such as `node_modules`, and binary or sensitive files skipped.
Loading finishes without automatically asking the model to summarize the entire
snapshot. This avoids an expensive inference before you have asked a useful
question. Set `"summarize_project_on_load": true` if you prefer an immediate summary.
Explicit questions using at least half the model context use `long_context_timeout`.

`buddy stop` from the shell (or `/stop` and Ctrl-C in the Buddy pane) stops the
current task, clears queued requests, and ignores any response that arrives later.
Term Buddy closes the streaming connection so compatible model servers also stop
generation. Starting project
learning also supersedes passive observation. Passive command observations never
run tools; autonomous tools are reserved for questions you explicitly ask.

With `"interrupt_on_new_question": true`, a new explicit question supersedes the
answer currently being generated. The partial answer and original request are passed
to the replacement request, allowing instructions such as “make that shorter.”
Completed questions and answers are retained as lightweight chat context. Rewrite,
summary, and “make that shorter” follow-ups use that chat context without resending
the loaded project or launching tools. `max_response_tokens` bounds each generation.

Session management:

```bash
term-buddy attach
term-buddy stop
term-buddy --session incident-42
term-buddy --no-watch
```

Creating a new tmux session starts a fresh chat and transcript. Reattaching to an
existing session restores its context without replaying old questions or tools.

## Read-only and yolo modes

By default, `/run` directly executes only an allowlist of inspection commands, with
no shell expansion, pipelines, redirects, or mutating Git/find/sed operations. This
is a safety boundary for the tool runner, not an operating-system sandbox. The model
can still read information those commands expose with your account's permissions.

To allow arbitrary executable commands:

```bash
term-buddy --yolo
```

Yolo mode bypasses the read-only allowlist. Use it only on hosts and inside working
directories where you accept the risk of model-suggested or manually entered writes.
Term Buddy never grants privileges beyond the user that launched it.

## Configuration

Generate the default private config:

```bash
term-buddy config
```

It is written to `~/.config/term-buddy/config.json`. Common settings:

```json
{
  "endpoint": "http://127.0.0.1:8080/v1",
  "model": "ornith",
  "api_key": "",
  "shell": "/bin/bash",
  "session_name": "term-buddy",
  "buddy_width": 42,
  "max_output_chars": 12000,
  "context_commands": 12,
  "request_timeout": 90,
  "long_context_timeout": 600,
  "max_response_tokens": 4096,
  "proactive": true,
  "autocomplete": false,
  "tools": true,
  "web": false,
  "context_window_tokens": 200000,
  "chars_per_token_estimate": 3.0,
  "project_context_fraction": 0.8,
  "summarize_project_on_load": false,
  "interrupt_on_new_question": true,
  "show_activity_panel": true,
  "activity_panel_height": 7,
  "optimize_operational_project_questions": true
}
```

Environment variables override the corresponding file values:

```bash
export TERM_BUDDY_ENDPOINT=http://127.0.0.1:8080/v1
export TERM_BUDDY_MODEL=ornith
export TERM_BUDDY_API_KEY=optional-secret
```

The endpoint and model can also be selected for a launch:

```bash
term-buddy --endpoint http://127.0.0.1:8080/v1 --model ornith
```

Global options must appear before subcommands, for example
`term-buddy --session incident attach`.

## Server notes

- Run the model on loopback, or protect a remotely bound endpoint with TLS and
  authentication.
- Terminal output can contain credentials. It is sent to the configured model and
  retained for the life of the local runtime session.
- Term Buddy keeps the complete raw pane stream in a private runtime
  `transcript.log` so reattached sessions retain their history. Only a bounded tail
  is sent with each model request. Long-running, high-output sessions can therefore
  consume disk space; stop the session and remove its runtime directory when the
  incident is finished.
- Commands that redraw the full terminal, nested tmux sessions, password prompts,
  and remote interactive SSH programs are not interpreted reliably. Proactive
  comments concern completed shell commands only.
- One Buddy session maps to one tmux session. Use `--session NAME` for parallel
  incidents.

## Development

Run the dependency-free test suite:

```bash
python3 -m unittest discover -v
```

## License

MIT
