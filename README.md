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

Autocomplete is off by default because it performs an inference while you edit. To
enable it, set `"autocomplete": true` in the configuration, restart the session,
then press **Shift-Tab** while editing a command. Term Buddy inserts the returned
suffix without pressing Enter.

The compact header is designed for a narrow server pane. The bottom line always shows
the current harness action, such as `RETRIEVE`, `TOOL`, `PREFILL`, or `GENERATE`.
Press **F2** for request size, elapsed time, tool activity, retrieval sources, and
generation progress. A structured copy is written to the private session file
`activity.jsonl`. Private model chain-of-thought is not displayed.

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

Successful commands never invoke the model. A single failure is recorded without
interrupting you. If the same failure occurs twice consecutively, Buddy requests one
short hint using the relevant recent terminal evidence. Identical model tool requests
are blocked on repetition to prevent loops.

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
