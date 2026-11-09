## `uv`

```shell
alias prtool='uv --verbose --project ~/projects/github-rpardini-public/prtool run ~/projects/github-rpardini-public/prtool/prtool.py'
```

# Use

`prtool` is a [Textual](https://textual.textualize.io/) TUI.

```
prtool                      # open the TUI
prtool <sha1> <a..b> ...    # same, with those commits preselected
prtool --list               # start on the PR history screen
prtool --reconfigure        # start on the settings screen
prtool --base develop       # one-off base override; also --no-fetch, -v (debug into the Log pane)
prtool --no-rebase          # don't offer to rebase when upstream is ahead (F6 still does)
```

The main screen shows:

- settings bar (upstream ← your fork, base, push remote, work branch); click it or press `F2` to edit
- commits ahead of the base (multi-select; `space` toggles, `a` all, `n` none), with badges for commits already in a PR;
  `[NOPR] ...` commits are listed but can't be selected
- PR form: target (new PR, or update an existing one), title (`ctrl+t` cycles commit subjects), branch, base, note
- live body preview, and a log of git/GitHub activity (`ctrl+l` toggles it)

Selecting commits whose subjects match an open, previously sent PR switches the target to "Update #N" and fills in its
title, branch, base and note. `ctrl+s` shows the plan and sends: cherry-picks onto the upstream base, force-pushes to your
fork, creates a draft PR or updates the existing one. `F3` shows history (`enter` loads a PR into the form, `o` opens it,
`d` drops it from history), `F5` fetches and reloads, `ctrl+o` opens the current PR.

When the upstream base is ahead of the work branch, prtool lists the new upstream commits and offers "Rebase if no
conflicts", dry-checked first with `git merge-tree` (no merges are ever made; the work branch stays flat). "Not now" isn't
asked again until upstream moves; `F6` offers it again. The settings bar shows `base +N` while behind.

On first run in a clone the settings screen opens, suggesting:

- the upstream repo (fork parent, `upstream` remote, ...)
- the base branch (from the upstream's default branch and the bases of its recent PRs)
- the remote to push PR branches to (your fork); it can add one

The upstream base is fetched by URL into `refs/prtool/upstream/<base>`; no `upstream` remote needed.

The optional note (Markdown bullets) is rendered as a blockquote atop the PR body.

## Storage

`~/.config/prtool/` (or `$XDG_CONFIG_HOME/prtool/`):

- `clones.yaml` — per-clone settings, keyed by the clone's absolute path
- `history/<owner>__<repo>.yaml` — PRs sent to that upstream, shared by all clones

## Code

- `prtool.py` — CLI entry point
- `prtool_tui.py` — the Textual app
- `prtool_core.py` — storage, git/GitHub helpers, PR body Markdown (no UI)
