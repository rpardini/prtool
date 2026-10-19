## `uv`

```shell
alias prtool='uv --verbose --project ~/projects/github-rpardini-public/prtool run ~/projects/github-rpardini-public/prtool/prtool.py'
```

# Use

```
prtool                      # interactive commit picker (base..HEAD)
prtool <sha1> <sha1> ...    # or explicit commits / ranges (a..b)
prtool --list               # PRs sent to this upstream, with live state
prtool --reconfigure        # re-run the settings wizard for this clone
prtool --base develop ...   # one-off base override; also --no-fetch, --no-rebase, -y, -v
```

On first run in a clone, prtool asks for (suggesting, allowing edit):

- the upstream repo (fork parent, `upstream` remote, ...)
- the base branch (from the upstream's default branch and the bases of its recent PRs)
- the remote to push PR branches to (your fork)

The upstream base is fetched by URL into `refs/prtool/upstream/<base>`; no `upstream` remote needed.

Every PR sent is recorded (title, custom-title flag, note, branch, base, commits, dates, PR#). Re-sending commits whose
subjects match an open recorded PR offers to update it, reusing its title/branch/note.

An optional note (Markdown bullets) is rendered as a blockquote atop the PR body.

## Storage

`~/.config/prtool/` (or `$XDG_CONFIG_HOME/prtool/`):

- `clones.yaml` — per-clone settings, keyed by the clone's absolute path
- `history/<owner>__<repo>.yaml` — PRs sent to that upstream, shared by all clones
