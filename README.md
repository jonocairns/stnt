# Stnt

**A local, isolated workspace for Amp that pauses when you leave and resumes
where you stopped.**

Stnt is pronounced “stint”: a bounded period of work on one task.

Run `stnt` from a Git repository. Stnt opens Amp in a private clone inside a
local Linux microVM, connects your editor, and opens your development service
when the project has one. Your host checkout stays unchanged.

When you are done for now, pause the workspace. Tomorrow, run `stnt` again and
continue with the same files, packages, containers, and Amp threads.

> [!IMPORTANT]
> Stnt is currently an early alpha for macOS on Apple Silicon. Its main
> workflow has been proven end to end, but project support is still narrow and
> Docker Sandboxes is experimental. Use it for dogfooding, not as
> install-and-forget tooling.

## The idea

Running Amp directly in a checkout gives it access to your machine. A
disposable environment gives you a stronger boundary, but throws away useful
state. Stnt keeps the boundary and retains the state.

Stnt gives Amp a retained environment with a private writable clone:

```text
your repository
      │ read-only source
      ▼
┌──────────────────────────────┐
│ local Linux microVM          │
│                              │
│ private Git clone            │
│ project tools and service    │
│ Amp threads and transcripts  │
└──────────────────────────────┘
```

The microVM is the workspace. Amp threads created inside it share the same
private clone. Pausing stops its active CPU and memory use without throwing the
workspace away.

## Get started

### 1. Check the prerequisites

Stnt currently requires:

- macOS on Apple Silicon
- [Amp](https://ampcode.com/) authenticated on the host
- Docker Sandboxes v0.38.0, with `sandboxd` running
- Git and `jq`
- a normal Git checkout with a named branch and committed `HEAD`

Stnt uses Docker's host-side credential proxy for the Amp API key. Setup will
check this and explain any action you need to take; it does not print or store
the credential value.

### 2. Install the development checkout

Stnt does not have a packaged installer yet. Clone this repository, then link
its launcher into your `PATH`:

```bash
git clone https://github.com/jonocairns/stnt.git
cd stnt
mkdir -p ~/.local/bin
ln -s "$(pwd)/bin/stnt" ~/.local/bin/stnt
stnt --help
```

If your shell cannot find `stnt`, add `~/.local/bin` to `PATH` and open a new
shell. The link points at the checkout, so updating the checkout updates the
command.

### 3. Run the one-time setup

```bash
stnt setup
```

Setup checks Amp, Docker Sandboxes, credentials, SSH, VS Code, and the optional
image-paste capability. It asks before installing Stnt's narrow SSH entry or
enabling clipboard image paste. For anything it cannot configure safely, it
prints the exact next command instead.

You can rerun setup at any time. It is designed to be idempotent.

### 4. Review a project

From the repository you want to work in:

```bash
cd ~/projects/my-project
stnt init
```

Stnt reads committed project declarations and proposes the environment it will
use: setup commands, an optional development service, local inputs, network
access, credentials, and VM resources. Review each item before accepting it.
Stnt does not execute project code during this review.

The reviewed profile is stored in your user configuration, not in the
repository. If relevant project files change later, Stnt fails closed and asks
you to review the differences with:

```bash
stnt reconfigure
```

Repository profiles are currently best suited to projects with a root Nix
flake and an aarch64-linux default development shell. A narrowly recognizable
Vite `dev` script can also provide automatic service startup. Other ordinary Git
repositories can still use the basic isolated Amp workspace, but automatic
toolchain and service setup may not be available yet.

### 5. Start working

```bash
stnt
```

On the first run, Stnt creates the private clone and prepares the workspace. On
later runs, the same command resumes it.

When configured, Stnt also:

1. restores and checks the development service;
2. opens its verified local URL;
3. opens the private clone in VS Code over Remote SSH; and
4. hands your terminal to Amp inside the microVM.

The first run is slower because it creates the VM, provisions the project, and
may cache the matching VS Code server. Warm resumes reuse that work.

## Your day-to-day workflow

### Work normally inside Amp

Once Amp opens, use its normal shortcuts and tools. In particular:

- create and switch Amp threads normally; they stay in this workspace;
- create your task branch inside the private clone when you know its name;
- commit and push from inside the workspace as usual, when GitHub access was
  approved during project review; and
- paste a screenshot with `Ctrl+V` when you enabled image paste during setup.

The path shown inside the microVM can look identical to your host repository
path. It is still a different, privately writable clone.

### Pause and resume

Quit Amp with its normal `Ctrl+C Ctrl+C` shortcut. Stnt then offers:

```text
Pause   stop and retain the complete workspace (default)
Finish  permanently discard the workspace and guest history
Cancel  return to Amp
```

Choose **Pause** for ordinary daily use. It keeps the private clone, installed
packages, containers, service state, and Amp history.

Resume later from the same host repository:

```bash
stnt
```

Pausing is also available from another terminal:

```bash
stnt pause
```

### Run parallel work

To create another independently writable workspace for the same repository:

```bash
stnt new
```

Each workspace has its own VM, clone, tools, containers, and Amp threads. If
you already have several, `stnt` shows a selector instead of guessing which one
you meant.

To begin from a specific existing local branch without switching your host
checkout:

```bash
stnt new --from my-existing-branch
```

### Finish work safely

In normal use, commit your work and push it before removing the workspace.

To preserve the current committed private-clone commit on a host-local branch
and then remove the workspace:

```bash
stnt detach
```

Detach requires a clean private clone and verifies the preserved commit before
removing anything. It never commits, stashes, resets, or discards work for you.

**Finish is destructive.** It permanently removes the private clone, guest Amp
threads, and transcripts without preserving Git work. Stnt requires you to type
the exact workspace ID before it proceeds. Prefer Pause or Detach unless you
deliberately want to discard the complete workspace.

## What Stnt changes and what it does not

Stnt owns the sandbox lifecycle so that you do not need to manage Docker
sandbox names, VM ports, generated Git remotes, or editor SSH targets.

It does **not**:

- modify or switch your host checkout;
- automatically commit, stash, push, merge, reset, or discard Git work;
- infer and execute arbitrary setup instructions without review; or
- copy credential values into Stnt state or the sandbox filesystem.

Clone mode prevents writes to your host checkout, but the sandbox can read the
host worktree—including local changes and ignored or untracked files—through
Docker's read-only source mount. Those changes are not copied into the private
clone, which starts from a pinned committed branch. Do not treat Stnt as a way
to hide repository-local secrets from Amp. Amp runs with broad permissions
inside the isolated microVM.

When Stnt cannot prove the state of a workspace, Git clone, service, or stop
operation, it retains the workspace and reports the next recovery command. It
does not replace or delete an ambiguous workspace.

## A few useful commands

Most days, you only need `stnt`, `stnt new`, and Pause.

```bash
stnt                 # create or resume
stnt new             # create an independent parallel workspace
stnt list            # list every retained workspace
stnt show            # interactively inspect and manage workspaces
stnt open            # reopen the selected verified service URL
stnt editor          # reconnect VS Code and enter the managed Amp lifecycle
stnt doctor          # read-only prerequisite and state diagnostics
stnt config show     # show the reviewed project profile and detected drift
stnt detach          # preserve committed work, then remove the workspace
```

Use `stnt --help` for recovery, destructive cleanup, stack experiments, and
exact `--session` selection. Those commands are deliberately outside the
everyday path.

## If something is not working

Start with the read-only diagnostic report:

```bash
stnt doctor
```

It checks the host, Git repository, Amp, Docker, credentials, SSH, editor,
state records, and retained sandboxes without starting or changing them.

For more detail from a failing lifecycle command:

```bash
stnt --verbose <command>
```

If creation was interrupted after Stnt recorded a workspace, use the exact
`recover-create` command printed in the error. Running plain `stnt` will not
silently create a replacement.

### VS Code does not connect

Install the Remote - SSH extension, then configure VS Code to download its
server on the host for the network-restricted microVM:

```json
"remote.SSH.localServerDownload": "always",
"remote.SSH.useExecServer": false
```

Restart VS Code after changing these settings. Terminal-only Amp use continues
to work when editor setup is unavailable.

## Current boundaries

Stnt is intentionally narrow while its daily workflow is being proven. The
current implementation supports one Docker runtime, ordinary Git
repositories, zero or one reviewed HTTP(S) service, VS Code Remote SSH, and
optional proxy-managed GitHub push access.

Notable unsupported cases include linked worktrees, submodules, Git LFS,
non-GitHub push, arbitrary project toolchains, multiple published services,
and non-Apple hosts. A fixed HTTPS project origin also means only one live
workspace can currently bind that origin's port.

## Development

Run the reproducible check suite with:

```bash
nix flake check
```

The direct local checks are:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
bin/phase0b-test
bin/phase0c-test
bin/stack-finish-test
```

Live Docker proofs are opt-in because they create and remove real local
microVMs. See the scripts under `bin/` before running them.

## License

Stnt is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE)
for third-party attribution.
