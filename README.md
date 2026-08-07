# term-buddy

```text
                                                             @%%%%%%@.
                                                        @@   %::::::*   @@
                                                     @%*=%   %::::::*   %=*%@
                                                   @*=:::%   %::::::*   %:::=*@
                                                   +:::::%   %::::::*   %:::::=%
                                                  %=:::::%   %::::::*   %::::::*
                                                 %=:::::=@   %::::::*   @=:::::=%
                                               %*=::::::*    %::::::*    #-:::::=*@
                                              %=:::::::=@    #::::::+    -+:::::::=%
                                            @*=:::::::=%     *::::::+=    @=:::::::=*@
                            @%%%%@@@+     @@#++++++++*@     ##++++++#.     @#********%@@     :@@@@@@@@
                          %+=::-*@       .           :                                          ::    --
                        #@*==+*@.     -@@%%%%%%%%%%%@-      @%%%%%%%@+      :*+****+*+*++#%:     .##+++*@=
                      *=    .*       :                               :                              .      -
                   :@%*==+#@       @%#***###******%@        @********@        *#*+++++*+*++*+*%:      @%*==*#*:
                  *=:  :+%:      +=:             .+         ::.   :  %                         ::       :=.   :=
               %%*=---+#+    @%%*+=-==--=====--=-*#         %--------%         =+::--:-::---::::-=++*    :+=:::--*=
             @*=::::=*@    #*=::::::::::::::::::=%          #::::::::#          %**=+=***+=+=++=+++**@.    @##+*+*#@@
           @*=:::::=%     %=-:::::::::::::::::::*           +::::::::*                                :-     -       ==
         @*=:::::==*    @*=::::::::::::::::::::=@          @=::::::::+=          @#+++++*++++++++++++==*#%    @#*=====*#@
       @*=:::::::= :#%%*=::::::::::::::::::::::*           %:::::::::==           *.::.: :.::..:::::..:::=*%%#  =:::::::=*@
    @@@@%%%%%%%%%@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%@           @%%%%%%%%%@@           +@@%%@@@%%%@%%%%%%@%%%%%%%%@@@@%%%%%%%%%@@@+
```

The original reusable artwork is available at
[assets/term-buddy-logo.txt](assets/term-buddy-logo.txt).

Term Buddy opens a tmux workspace with your Bash shell on the left and a local-AI
debugging companion on the right. It quietly records completed commands, offers a hint
after a failure repeats, answers explicit questions, remembers indexed projects, and
can run read-only diagnostics of its own.

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
- `/learn` updates local project memory without calling the model.
- `/autocomplete on|off` toggles Shift-Tab completion for this session.
- `/watch on|off` toggles repeated-command hints for this session.
- `/context on|off` toggles recovery of questions typed directly into the shell.
- `/log` shows the structured harness-log path.
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

Inline completion is off by default.
Press **F2**, then **A**, or enter `/autocomplete on` in the Buddy pane to enable it
immediately. As you type, Term Buddy scans live local state and draws its best suffix
directly in front of the cursor using dim text. The ghost is visual-only: ignoring it
and pressing Enter executes exactly what you typed. Press **Shift-Tab** to copy the
visible suffix into Bash's real editable buffer without pressing Enter. Live toggles
last for the current session and do not rewrite configuration.
From the left shell, `buddy autocomplete on` and `buddy autocomplete status` control
the same live setting without involving the model. The header must say `comp+`.
The live preview completes directories for `cd`, files for command arguments, and
shell built-ins or executables for the first word. Nested prefixes such as
`cd repos/ter` scan that directory. Typing `git commit -m ` previews a short message
derived from staged changes. Previewing is debounced, local, and requires no GPU
inference. Shift-Tab never waits for Ornith; it returns immediately when no local
suggestion exists.

The shell runs through a transparent PTY input adapter so ghost text never enters the
executable command. The adapter activates only at a marked Bash prompt; after Enter,
interactive programs such as editors, SSH, pagers, and password prompts receive raw
terminal passthrough. Cursor movement or unsupported input safely suppresses preview
until the next prompt rather than guessing the state of the command line.

The compact header is designed for a narrow server pane. The bottom line always shows
the current harness action, such as `RETRIEVE`, `TOOL`, `PREFILL`, or `GENERATE`.
Press **F2** for a compact menu that toggles autocomplete, repeated-command hints,
forgotten-`buddy` questions, and detailed activity. Detailed activity includes request
size, elapsed time, tool activity, retrieval sources, and generation progress. A
structured copy is written to the private session file
`activity.jsonl`. Private model chain-of-thought is not displayed.
At startup, Buddy temporarily expands across the complete tmux window and clears it
for a four-second logo splash. It then restores the normal shell/Buddy split, leaving
your shell startup tools (such as hardware/system summaries) intact. Small terminals
crop the artwork instead of hiding it, and any key dismisses it immediately.

To build or refresh local project memory, run this from the left shell:

```bash
buddy learn project
```

This indexes text locally, skips dependencies, build output, binaries, and likely
secrets, and stores file hashes so later runs only update changed files. It performs
no inference and sends nothing to Ornith. Ask normally afterward:

```bash
buddy what should I change in ScrapForm.vue to make it green?
```

Term Buddy automatically retrieves only relevant files. Ordinary shell and Git
questions remain lightweight:

```bash
buddy how do I run this on port 3005?
buddy what is the latest Git commit?
```

Common Git, GPU, port, disk, and memory questions use deterministic read-only
diagnostics before the model sees the request. The model receives their authoritative
output instead of having to invent the correct inspection command.

Ordinary successful commands never invoke the model. Running the exact same command
twice consecutively is treated as a possible sign that you are stuck—even when it
succeeds—and requests one short hint. A repeated matching failure does the same.
The live watcher only holds this sequence in memory; it does not modify project
memory. Identical model tool requests are blocked on repetition to prevent loops.
Bash command capture remains repetition-aware when `HISTCONTROL` uses `ignoredups`
or `erasedups`; blank Enter presses are not mistaken for repeated commands. The
compact header shows `comp on` or `comp off` so autocomplete state is always visible.

Context-question mode is session-only and disabled by default. Enable it with F2 then
Q, `/context on` in the Buddy pane, or `buddy context on` in the shell. Term Buddy
continues recording bounded recent command context, says nothing about valid commands,
and answers a failed line only when it clearly looks like a natural-language question
such as `why is this service failing?`. Ordinary typos such as `gti status` remain
normal shell errors. The compact header shows `ask+` while this mode is enabled.

`buddy stop` from the shell (or `/stop` and Ctrl-C in the Buddy pane) stops the
current task, clears queued requests, and ignores any response that arrives later.
Term Buddy closes the streaming connection so compatible model servers also stop
generation. HTTP reader errors caused by closing a stream are contained in the
background worker and never written over the TUI. Starting project
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

It is written to `~/.config/term-buddy/config.json`. The generated configuration is
intentionally small; context sizes, retrieval budgets, and inference routing are
automatic:

```json
{
  "endpoint": "http://127.0.0.1:8080/v1",
  "model": "ornith",
  "api_key": "",
  "shell": "/bin/bash",
  "session_name": "term-buddy",
  "buddy_width": 42
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
