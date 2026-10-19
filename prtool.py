import argparse
import logging
import os
import random
import re
import readline  # noqa: F401 -- gives input() line editing (arrows, history)
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from itertools import islice
from pathlib import Path

import yaml
from git import GitCommandError, InvalidGitRepositoryError, Repo
from github import Auth, Github, GithubException
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()
logger = logging.getLogger("prtool")

PR_STATE_STYLES = {"open": "green", "draft": "dim green", "merged": "magenta", "closed": "red", "unknown": "dim"}


def die(message: str):
	console.print(f"[bold red]✗[/] {message}")
	sys.exit(1)


def step(message: str):
	console.print(f"[bold cyan]•[/] {message}")


def now_iso() -> str:
	return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------------------------------
# Config & history storage, in ~/.config/prtool (or $XDG_CONFIG_HOME/prtool)
# - clones.yaml: per-clone settings, keyed by the clone's absolute path
# - history/<owner>__<repo>.yaml: PRs sent to an upstream repo, shared by all clones of it


def config_dir() -> Path:
	base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
	return Path(base) / "prtool"


def load_yaml(path: Path, default):
	if not path.exists():
		return default
	with path.open() as f:
		data = yaml.safe_load(f)
	return default if data is None else data


def save_yaml(path: Path, data):
	path.parent.mkdir(parents=True, exist_ok=True)
	# atomic: write to a temp file in the same directory, then rename over
	fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
	try:
		with os.fdopen(fd, "w") as f:
			yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=1000)
		os.replace(tmp_name, path)
	except BaseException:
		os.unlink(tmp_name)
		raise


def clones_file() -> Path:
	return config_dir() / "clones.yaml"


def load_clone_config(clone_path: str) -> dict | None:
	return load_yaml(clones_file(), {}).get(clone_path)


def save_clone_config(clone_path: str, cfg: dict):
	clones = load_yaml(clones_file(), {})
	clones[clone_path] = cfg
	save_yaml(clones_file(), clones)


class History:
	def __init__(self, upstream: str):
		self.upstream = upstream
		self.path = config_dir() / "history" / f"{upstream.replace('/', '__')}.yaml"
		self.records: list[dict] = load_yaml(self.path, {}).get("prs", [])

	def save(self):
		save_yaml(self.path, {"upstream": self.upstream, "prs": self.records})

	def get(self, number: int) -> dict | None:
		return next((r for r in self.records if r.get("number") == number), None)

	def upsert(self, record: dict):
		existing = self.get(record["number"])
		if existing is None:
			self.records.append(record)
		else:
			record["created"] = existing.get("created", record.get("created"))
			existing.clear()
			existing.update(record)
		self.save()

	def pr_by_subject(self) -> dict[str, dict]:
		# latest record wins for each commit subject
		result = {}
		for record in sorted(self.records, key=lambda r: r.get("updated", "")):
			for commit in record.get("commits", []):
				result[commit["subject"]] = record
		return result


# ---------------------------------------------------------------------------------------------------------------------
# GitHub helpers


def github_login() -> tuple[Github, str]:
	with console.status("Getting GitHub token from 'gh auth token'..."):
		try:
			out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout
		except FileNotFoundError:
			die("'gh' (GitHub CLI) not found in PATH.")
		except subprocess.CalledProcessError as e:
			die(f"'gh auth token' failed; run 'gh auth login' first.\n{e.stderr.strip()}")
		token = out.strip().splitlines()[0]
		github = Github(auth=Auth.Token(token))
		login = github.get_user().login
	step(f"Logged in to GitHub as [bold]{login}[/] (token '{token[1:5]}....')")
	return github, login


def parse_github_slug(url: str) -> str | None:
	"""'git@github.com:o/r.git', 'https://github.com/o/r', 'ssh://git@github.com/o/r.git' -> 'o/r'"""
	m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
	return f"{m[1]}/{m[2]}" if m else None


def pr_state(pr) -> str:
	if pr.merged:
		return "merged"
	if pr.state == "closed":
		return "closed"
	return "draft" if pr.draft else "open"


def styled_state(state: str) -> str:
	return f"[{PR_STATE_STYLES.get(state, 'dim')}]{state}[/]"


# ---------------------------------------------------------------------------------------------------------------------
# Configuration wizard: suggest, let the user confirm/edit, store.


def ask_upstream(github: Github, git_repo: Repo, current: str | None):
	candidates: list[tuple[str, str]] = []

	def add(slug, why):
		if slug and slug.lower() not in [c[0].lower() for c in candidates]:
			candidates.append((slug, why))

	if current:
		add(current, "current setting")

	remote_repos = []
	with console.status("Inspecting remotes on GitHub..."):
		for remote in git_repo.remotes:
			slug = parse_github_slug(remote.url)
			if not slug:
				continue
			try:
				remote_repos.append((remote.name, slug, github.get_repo(slug)))
			except GithubException as e:
				logger.debug(f"Remote '{remote.name}' ({slug}) not accessible: {e}")
	# fork parents/sources are the most reliable hint, then an 'upstream' remote, then whatever else is there
	for name, slug, gh_repo in remote_repos:
		if gh_repo.fork and gh_repo.parent is not None:
			add(gh_repo.parent.full_name, f"parent of fork {slug} (remote '{name}')")
			if gh_repo.source is not None:
				add(gh_repo.source.full_name, f"root of fork {slug} (remote '{name}')")
	for name, slug, gh_repo in remote_repos:
		if name == "upstream":
			add(gh_repo.full_name, "remote 'upstream'")
	for name, slug, gh_repo in remote_repos:
		add(gh_repo.full_name, f"remote '{name}'")

	table = Table(title="Upstream repo candidates", title_justify="left")
	table.add_column("#", justify="right", style="cyan")
	table.add_column("Repo", style="bold")
	table.add_column("Why", style="dim")
	for i, (slug, why) in enumerate(candidates, 1):
		table.add_row(str(i), slug, why)
	if candidates:
		console.print(table)

	while True:
		answer = Prompt.ask("Upstream repo the PRs go to ([cyan]#[/] or owner/repo)", default="1" if candidates else None)
		if answer.isdigit() and 1 <= int(answer) <= len(candidates):
			answer = candidates[int(answer) - 1][0]
		try:
			with console.status(f"Checking '{answer}'..."):
				return github.get_repo(answer)
		except GithubException:
			console.print(f"[red]Repo '{answer}' not found / not accessible, try again.[/]")


def ask_base(upstream_repo, current: str | None) -> str:
	counts = Counter()
	with console.status(f"Looking at recent PRs on {upstream_repo.full_name} to guess the base branch..."):
		for pr in islice(upstream_repo.get_pulls(state="all", sort="created", direction="desc"), 30):
			counts[pr.base.ref] += 1
	default_branch = upstream_repo.default_branch

	table = Table(title=f"Base branches used by the last {sum(counts.values())} PRs", title_justify="left")
	table.add_column("Branch", style="bold")
	table.add_column("PRs", justify="right", style="cyan")
	table.add_column("", style="dim")
	branches = [b for b, _ in counts.most_common()]
	if default_branch not in branches:
		branches.append(default_branch)
	for branch in branches:
		notes = []
		if branch == default_branch:
			notes.append("default branch")
		if branch == current:
			notes.append("current setting")
		table.add_row(branch, str(counts.get(branch, 0)), ", ".join(notes))
	console.print(table)

	suggestion = current or (counts.most_common(1)[0][0] if counts else default_branch)
	while True:
		answer = Prompt.ask("Base branch for incoming PRs", default=suggestion)
		try:
			upstream_repo.get_branch(answer)
			return answer
		except GithubException:
			console.print(f"[red]Branch '{answer}' doesn't exist in {upstream_repo.full_name}, try again.[/]")


def ask_push_remote(git_repo: Repo, login: str, upstream_repo, current: str | None) -> str:
	remotes = {r.name: r for r in git_repo.remotes}
	mine = [
		name
		for name, r in remotes.items()
		if (slug := parse_github_slug(r.url)) and slug.split("/")[0].lower() == login.lower()
	]

	table = Table(title="Local remotes", title_justify="left")
	table.add_column("Remote", style="bold")
	table.add_column("URL")
	table.add_column("", style="dim")
	for name, r in remotes.items():
		table.add_row(name, r.url, "yours" if name in mine else "")
	console.print(table)

	default = current if current in remotes else (mine[0] if mine else login)
	while True:
		name = Prompt.ask("Remote to push PR branches to (your fork)", default=default)
		if name in remotes:
			return name
		url = Prompt.ask(f"Remote '{name}' doesn't exist; URL to add it with", default=f"git@github.com:{login}/{upstream_repo.name}.git")
		if Confirm.ask(f"Add remote '{name}' → {url}?", default=True):
			git_repo.create_remote(name, url)
			return name


def configure(github: Github, git_repo: Repo, login: str, clone_path: str, current: dict | None) -> dict:
	current = current or {}
	console.print(Rule(f"[bold]Configuring prtool for {clone_path}"))
	while True:
		upstream_repo = ask_upstream(github, git_repo, current.get("upstream"))
		base = ask_base(upstream_repo, current.get("base"))
		push_remote = ask_push_remote(git_repo, login, upstream_repo, current.get("push_remote"))
		cfg = {"upstream": upstream_repo.full_name, "base": base, "push_remote": push_remote, "configured_at": now_iso()}

		grid = Table.grid(padding=(0, 2))
		grid.add_column(style="bold cyan")
		grid.add_column()
		grid.add_row("Upstream", cfg["upstream"])
		grid.add_row("Base branch", cfg["base"])
		grid.add_row("Push remote", f"{push_remote} ({git_repo.remotes[push_remote].url})")
		console.print(Panel(grid, title="Settings", subtitle=str(clones_file()), expand=False))
		if Confirm.ask("Save these settings?", default=True):
			save_clone_config(clone_path, cfg)
			return cfg
		current = cfg


# ---------------------------------------------------------------------------------------------------------------------
# Git helpers


def upstream_ref(base: str) -> str:
	return f"refs/prtool/upstream/{base}"


def ref_exists(git_repo: Repo, ref: str) -> bool:
	try:
		git_repo.git.rev_parse("--verify", "-q", ref)
		return True
	except GitCommandError:
		return False


def fetch_base(git_repo: Repo, upstream: str, base: str):
	# Fetch by URL into a private ref, so no correctly-named/pointed 'upstream' remote is needed.
	# Prefer the URL of a local remote pointing at upstream (keeps its auth method), otherwise https.
	url = next(
		(r.url for r in git_repo.remotes if (parse_github_slug(r.url) or "").lower() == upstream.lower()),
		f"https://github.com/{upstream}.git",
	)
	with console.status(f"Fetching {upstream} '{base}'..."):
		git_repo.git.fetch(url, f"+refs/heads/{base}:{upstream_ref(base)}")
	step(f"Fetched [bold]{upstream}[/] '{base}' → {upstream_ref(base)}")


def ensure_base(git_repo: Repo, upstream: str, base: str, fetch: bool):
	if fetch or not ref_exists(git_repo, upstream_ref(base)):
		fetch_base(git_repo, upstream, base)


def resolve_revisions(git_repo: Repo, revisions: list[str]) -> list:
	commits = []
	for revision in revisions:
		if ".." in revision:
			commits.extend(reversed(list(git_repo.iter_commits(revision))))  # oldest first
		else:
			commits.append(git_repo.commit(revision))
	seen = set()
	return [c for c in commits if not (c.hexsha in seen or seen.add(c.hexsha))]


def subject_of(commit) -> str:
	return commit.message.splitlines()[0].strip()


def parse_selection(text: str, count: int) -> list[int]:
	text = text.strip().lower()
	if text in ("a", "all"):
		return list(range(count))
	indexes = set()
	for part in re.split(r"[,\s]+", text):
		if not part:
			continue
		m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
		if not m:
			raise ValueError(f"can't parse '{part}'")
		start, end = sorted((int(m[1]), int(m[2] or m[1])))
		if start < 1 or end > count:
			raise ValueError(f"'{part}' out of range 1-{count}")
		indexes.update(range(start - 1, end))
	if not indexes:
		raise ValueError("nothing selected")
	return sorted(indexes)


def pick_commits(git_repo: Repo, base_ref: str, history: History) -> list:
	commits = list(reversed(list(git_repo.iter_commits(f"{base_ref}..HEAD", no_merges=True))))
	if not commits:
		die(f"No commits in {base_ref}..HEAD; nothing to PR.")
	by_subject = history.pr_by_subject()

	table = Table(title=f"Commits ahead of {base_ref}", title_justify="left")
	table.add_column("#", justify="right", style="cyan")
	table.add_column("SHA", style="yellow")
	table.add_column("Subject")
	table.add_column("PR")
	for i, commit in enumerate(commits, 1):
		record = by_subject.get(subject_of(commit))
		pr_col = f"#{record['number']} {styled_state(record.get('state', 'unknown'))}" if record else ""
		table.add_row(str(i), commit.hexsha[:10], subject_of(commit), pr_col)
	console.print(table)

	while True:
		answer = Prompt.ask("Commits to PR ([cyan]1-3,5[/], [cyan]a[/]=all, [cyan]q[/]=quit)")
		if answer.strip().lower() == "q":
			sys.exit(0)
		try:
			return [commits[i] for i in parse_selection(answer, len(commits))]
		except ValueError as e:
			console.print(f"[red]{e}[/]")


# ---------------------------------------------------------------------------------------------------------------------
# Markdown massaging


# Beautify, as commits might be word-wrapped, and that looks horrible on GitHub.
# Split the commits_markdown in to lines
# For each line, if it doesn't start (optionally space prefixed) "dash", trim leading spaces, and join to the previous line.
def rewrap_lines(markdown: str) -> str:
	lines = markdown.splitlines()
	rewrapped_lines = []
	previous_line = ""
	for line in lines:
		stripped_line = line.lstrip()
		# if the line start with "Signed-off-by:", completely skip it; also 'Assisted-by:' and 'Reviewed-by:' and 'Co-authored-by:'
		if stripped_line.startswith("Signed-off-by:") or stripped_line.startswith("Assisted-by:") or stripped_line.startswith("Reviewed-by:") or stripped_line.startswith("Co-authored-by:"):
			continue

		if stripped_line.startswith("- "):
			if previous_line:
				rewrapped_lines.append(previous_line)
			previous_line = line
		else:
			previous_line += " " + stripped_line
	if previous_line:
		rewrapped_lines.append(previous_line)
	return "\n".join(rewrapped_lines)


def rewrap_markdown(markdown: str) -> str:
	return emojify_first_level_bullet_points(rewrap_lines(markdown))


# Add emojis to first level bullet points; cycle through the emoji list and use them for each bullet point.
# Use generic, commonly used emojis that don't mean much; favor the plant-based ones (greenery is good).
emoji_list = ["🌱", "🌿", "🍃", "🍀", "🐸", "🌳", "🌴", "🌵"]

# randomize the order of the emojis
random.shuffle(emoji_list)


def emojify_first_level_bullet_points(markdown: str) -> str:
	lines = markdown.splitlines()
	emojified_lines = []
	emoji_index = 0
	for line in lines:
		stripped_line = line  # don't strip otherwise all levels are affected
		if stripped_line.startswith("- "):
			emoji = emoji_list[emoji_index % len(emoji_list)]
			emoji_index += 1
			new_line = line.replace("- ", f"- {emoji} ", 1)
			emojified_lines.append(new_line)
		else:
			emojified_lines.append(line)
	return "\n".join(emojified_lines)


def build_body(pr_title: str, commit_messages: list[str], note: list[str]) -> str:
	markdown_commit_messages = []
	# If only a single commit, do a short version, otherwise PRs spell out the same stuff many times
	if len(commit_messages) == 1:
		other_lines = [line for line in commit_messages[0].splitlines()[1:] if line.strip()]
		markdown_commit_messages.extend(other_lines)
		full_markdown = rewrap_markdown("\n".join(markdown_commit_messages))
	else:
		# Each title is a Markdown list item, and any other lines are indented under it.
		for commit_message in commit_messages:
			title = commit_message.splitlines()[0].strip()
			other_lines = [f"  {line}" for line in commit_message.splitlines()[1:] if line.strip()]
			markdown_commit_messages.append(f"- {title}")
			markdown_commit_messages.extend(other_lines)
		full_markdown = f"#### {pr_title}\n\n{rewrap_markdown(chr(10).join(markdown_commit_messages))}"

	note_markdown = render_note(note)
	return f"{note_markdown}\n\n{full_markdown}" if note_markdown else full_markdown


# ---------------------------------------------------------------------------------------------------------------------
# PR note: optional Markdown bullets, rendered as a blockquote atop the PR body


def normalize_note(lines: list[str]) -> list[str]:
	# every line becomes a bullet, unless it already is one (keeps nested '  - ' indents)
	return [line.rstrip() if line.lstrip().startswith("- ") else f"- {line.strip()}" for line in lines if line.strip()]


def render_note(note: list[str]) -> str:
	if not note:
		return ""
	return "\n".join(f"> {line}" for line in rewrap_lines("\n".join(note)).splitlines())


def edit_note_in_editor(note: list[str]) -> list[str]:
	editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
	with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="prtool-note-", delete=False) as f:
		f.write("\n".join(note) + "\n")
		f.write("# PR note, as Markdown bullets. Lines starting with '#' are ignored. Empty = no note.\n")
		path = f.name
	try:
		subprocess.run([*shlex.split(editor), path], check=True)
		lines = [line for line in Path(path).read_text().splitlines() if not line.startswith("#")]
	finally:
		os.unlink(path)
	# keep wrapped continuation lines; only force a bullet onto the very first line
	lines = [line for line in lines if line.strip()]
	if lines and not lines[0].lstrip().startswith("- "):
		lines[0] = f"- {lines[0].strip()}"
	return lines


def ask_note(previous: list[str]) -> list[str]:
	if previous:
		console.print(Panel(Markdown(render_note(previous)), title="Previous note", title_align="left", expand=False))
		hint = "[cyan]Enter[/]=keep, [cyan]-[/]=clear, [cyan]e[/]=$EDITOR, or type a new note"
	else:
		hint = "[cyan]Enter[/]=none, [cyan]e[/]=$EDITOR, or type it; one bullet per line, empty line ends"
	console.print(f"[bold]Note[/] ({hint}):")
	first = input("  > ")
	if not first.strip():
		return previous
	if first.strip() == "-" and previous:
		return []
	if first.strip() == "e":
		return edit_note_in_editor(previous)
	lines = [first]
	while line := input("  > "):
		lines.append(line)
	return normalize_note(lines)


# ---------------------------------------------------------------------------------------------------------------------
# Title & matching to previously sent PRs


def choose_title(subjects: list[str]) -> str:
	if len(subjects) == 1:
		return subjects[0]
	table = Table(title="Select a commit message for the PR title", title_justify="left")
	table.add_column("#", justify="right", style="cyan", no_wrap=True)
	table.add_column("Commit message", style="magenta")
	for index, subject in enumerate(subjects, 1):
		table.add_row(str(index), subject)
	table.add_row("n", "[italic]Type a new title[/]")
	table.add_row("q", "[italic]Quit[/]")
	console.print(table)
	choice = Prompt.ask("Title", choices=[str(i) for i in range(1, len(subjects) + 1)] + ["n", "q"], default="1")
	if choice == "q":
		sys.exit(0)
	if choice == "n":
		return Prompt.ask("Type a new title").strip()
	return subjects[int(choice) - 1]


def find_history_match(history: History, upstream_repo, subjects: list[str]):
	"""Find an open, previously-sent PR sharing commit subjects (they survive rebases/amends, SHAs don't)."""
	scored = []
	for record in history.records:
		overlap = len(set(subjects) & {c["subject"] for c in record.get("commits", [])})
		if overlap and record.get("state") not in ("merged", "closed"):
			scored.append((overlap, record.get("updated", ""), record))
	scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
	for overlap, _, record in scored:
		try:
			with console.status(f"Checking PR #{record['number']}..."):
				pr = upstream_repo.get_pull(record["number"])
		except GithubException:
			continue
		record["state"] = pr_state(pr)
		history.save()
		if record["state"] in ("open", "draft"):
			return record, pr, overlap
	return None


def normalize_branch(title: str) -> str:
	# Replace spaces with dashes, and remove any non-alphanumeric characters.
	normalized = "".join(c for c in title.replace(" ", "-") if c.isalnum() or c == "-")
	return "pr/" + normalized


# ---------------------------------------------------------------------------------------------------------------------
# Commands


def show_history(history: History, upstream_repo):
	if not history.records:
		console.print(f"No PRs recorded for {upstream_repo.full_name} yet ({history.path}).")
		return
	table = Table(title=f"PRs sent to {upstream_repo.full_name}", title_justify="left")
	table.add_column("PR", justify="right", style="cyan")
	table.add_column("State")
	table.add_column("Title")
	table.add_column("Branch → base", style="dim")
	table.add_column("Commits", justify="right")
	table.add_column("Updated", style="dim")
	with console.status("Refreshing PR states from GitHub..."):
		for record in history.records:
			try:
				record["state"] = pr_state(upstream_repo.get_pull(record["number"]))
			except GithubException:
				record["state"] = "unknown"
	history.save()
	for record in sorted(history.records, key=lambda r: r.get("updated", ""), reverse=True):
		title = Text(record["title"], style=f"link {record['url']}")
		if record.get("title_custom"):
			title.append(" ✎", style="dim")
		table.add_row(
			f"#{record['number']}",
			styled_state(record["state"]),
			title,
			f"{record['head_branch']} → {record['base']}",
			str(len(record.get("commits", []))),
			record.get("updated", "")[:16].replace("T", " "),
		)
	console.print(table)


def push_pr_branch(git_repo: Repo, work_branch: str, pr_branch: str, base_ref: str, shas: list[str], push_remote: str):
	try:
		with console.status(f"Creating '{pr_branch}' from {base_ref}..."):
			git_repo.git.checkout("-B", pr_branch, base_ref)
		for sha in shas:
			with console.status(f"Cherry-picking {sha[:10]}..."):
				git_repo.git.cherry_pick(sha)
		with console.status(f"Force-pushing '{pr_branch}' to '{push_remote}'..."):
			git_repo.git.push("-f", push_remote, pr_branch)
		step(f"Pushed [bold]{pr_branch}[/] to '{push_remote}'")
	except GitCommandError:
		try:
			git_repo.git.cherry_pick("--abort")
		except GitCommandError:
			pass
		raise
	finally:
		git_repo.git.checkout(work_branch)


def parse_args():
	parser = argparse.ArgumentParser(prog="prtool", description="Dispatch GitHub PRs from Markdown-written commits.")
	parser.add_argument("revisions", nargs="*", help="commits or ranges (a..b) to PR; none = interactive picker")
	parser.add_argument("--reconfigure", action="store_true", help="re-run the settings wizard for this clone")
	parser.add_argument("--list", action="store_true", help="list PRs sent to this upstream, with live state")
	parser.add_argument("--base", help="one-off base branch override (not stored)")
	parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="don't fetch the upstream base")
	parser.add_argument("--no-rebase", dest="rebase", action="store_false", help="don't rebase the work branch")
	parser.add_argument("-y", "--yes", action="store_true", help="don't ask for final confirmation")
	parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
	return parser.parse_args()


def main():
	args = parse_args()
	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.WARNING,
		format="%(message)s",
		datefmt="[%X]",
		handlers=[RichHandler(rich_tracebacks=True, console=console)],
	)

	try:
		git_repo = Repo(Path.cwd(), search_parent_directories=True)
	except InvalidGitRepositoryError:
		die(f"'{Path.cwd()}' is not inside a git repository.")
	clone_path = str(Path(git_repo.working_tree_dir).resolve())
	console.print(Rule(f"[bold]prtool[/] · {clone_path}"))

	github, login = github_login()

	cfg = load_clone_config(clone_path)
	if cfg is None or args.reconfigure:
		cfg = configure(github, git_repo, login, clone_path, cfg)
		if args.reconfigure and not args.revisions:
			return
	upstream_repo = github.get_repo(cfg["upstream"])
	base = args.base or cfg["base"]
	push_remote = cfg["push_remote"]
	if push_remote not in [r.name for r in git_repo.remotes]:
		die(f"Push remote '{push_remote}' doesn't exist anymore; run with --reconfigure.")
	fork_owner = (parse_github_slug(git_repo.remotes[push_remote].url) or login).split("/")[0]
	step(f"Upstream [bold]{upstream_repo.full_name}[/] base [bold]{base}[/], pushing to '{push_remote}' ({fork_owner})")

	history = History(upstream_repo.full_name)
	if args.list:
		show_history(history, upstream_repo)
		return

	# Make sure we've a clean working copy, no pending changes are allowed.
	if git_repo.is_dirty():
		die("Working copy is dirty. Commit or stash your changes first; squash fixups and amend commits as needed.")
	if git_repo.head.is_detached:
		die("HEAD is detached; checkout your work branch first.")
	work_branch = git_repo.active_branch.name
	step(f"Work branch [bold]{work_branch}[/]")

	ensure_base(git_repo, upstream_repo.full_name, base, args.fetch)
	base_ref = upstream_ref(base)

	# Rebase the work branch on top of the upstream branch, for sanity; revert cleanly on conflicts.
	if args.rebase:
		try:
			with console.status(f"Rebasing '{work_branch}' onto {base_ref}..."):
				git_repo.git.rebase(base_ref, work_branch)
		except GitCommandError as e:
			try:
				git_repo.git.rebase("--abort")
			except GitCommandError:
				pass
			die(f"Rebase of '{work_branch}' onto {base_ref} failed and was aborted; resolve manually.\n{e.stderr}")
		step(f"Rebased '{work_branch}' onto {base_ref}")

	console.print(Rule("Commits"))
	commits = resolve_revisions(git_repo, args.revisions) if args.revisions else pick_commits(git_repo, base_ref, history)
	for commit in commits:
		# make sure the commit is in the work branch, not a floating/dangling commit
		if not git_repo.is_ancestor(commit.hexsha, work_branch):
			die(f"Commit '{commit.hexsha}' not found in work branch '{work_branch}'.")
	subjects = [subject_of(c) for c in commits]
	commit_messages = [c.message for c in commits]

	console.print(Rule("Pull request"))
	existing_pr, record = None, None
	pr_title, pr_branch, pr_base = None, None, base
	match = find_history_match(history, upstream_repo, subjects)
	if match:
		record, existing_pr, overlap = match
		console.print(
			Panel(
				f"[bold]#{existing_pr.number}[/] {record['title']}\n[dim]{existing_pr.html_url}[/]\n"
				f"{overlap}/{len(subjects)} selected commits match; branch {record['head_branch']} → {existing_pr.base.ref}",
				title="Previously sent PR found",
				title_align="left",
				expand=False,
			)
		)
		choice = Prompt.ask("[cyan]u[/]pdate it, send as [cyan]n[/]ew PR, or [cyan]q[/]uit", choices=["u", "n", "q"], default="u")
		if choice == "q":
			sys.exit(0)
		if choice == "u":
			pr_title = Prompt.ask("PR title", default=record["title"]).strip()
			pr_branch = record["head_branch"]
			pr_base = existing_pr.base.ref
		else:
			existing_pr, record = None, None

	if pr_title is None:
		pr_title = choose_title(subjects)
		pr_branch = normalize_branch(pr_title)
		# Fall back to finding an open PR with the same branch name, as before history existed.
		with console.status("Looking for an open PR with the same branch..."):
			existing_pr = next(iter(upstream_repo.get_pulls(state="open", head=f"{fork_owner}:{pr_branch}")), None)
		if existing_pr is not None:
			record = history.get(existing_pr.number)
			pr_base = existing_pr.base.ref
			step(f"Found existing open PR [bold]#{existing_pr.number}[/] for branch '{pr_branch}'")

	if not pr_title:
		die("Empty title.")
	if pr_branch == work_branch:
		die(f"PR branch '{pr_branch}' is the work branch itself; pick another title.")

	note = ask_note(record.get("note", []) if record else [])
	full_markdown = build_body(pr_title, commit_messages, note)

	if pr_base != base:
		ensure_base(git_repo, upstream_repo.full_name, pr_base, True)
	pr_base_ref = upstream_ref(pr_base)

	# Great, now show the user what we're about to do, and ask for confirmation.
	grid = Table.grid(padding=(0, 2))
	grid.add_column(style="bold cyan", no_wrap=True)
	grid.add_column()
	grid.add_row("Upstream", f"{upstream_repo.full_name} ← {fork_owner}:{pr_branch}")
	grid.add_row("Base", f"{pr_base} ({pr_base_ref})")
	if existing_pr is not None:
		grid.add_row("Action", f"[yellow]update[/] #{existing_pr.number} {existing_pr.html_url}")
	else:
		grid.add_row("Action", "[green]create draft PR[/]")
	grid.add_row("Title", f"[bold]{pr_title}[/]")
	grid.add_row("Commits", "\n".join(f"[yellow]{c.hexsha[:10]}[/] {subject_of(c)}" for c in commits))
	console.print(Panel(grid, title="Plan", title_align="left"))
	console.print(Panel(Markdown(full_markdown), title="PR body", title_align="left"))
	logger.debug(f"Raw markdown:\n{full_markdown}")

	if not args.yes and not Confirm.ask("Proceed?", default=False):
		console.print("Aborted.", style="bold red")
		sys.exit(1)

	console.print(Rule("Sending"))
	try:
		push_pr_branch(git_repo, work_branch, pr_branch, pr_base_ref, [c.hexsha for c in commits], push_remote)
	except GitCommandError as e:
		die(f"Git failed; back on '{work_branch}'.\n{e}")

	with console.status("Talking to GitHub..."):
		if existing_pr is not None:
			existing_pr.edit(title=pr_title, body=full_markdown)
			pr = existing_pr
			action = "updated"
		else:
			pr = upstream_repo.create_pull(
				maintainer_can_modify=False,  # dont leak my secrets
				title=pr_title,
				body=full_markdown,
				head=f"{fork_owner}:{pr_branch}",
				base=pr_base,
				draft=True,
			)
			action = "created"

	history.upsert(
		{
			"number": pr.number,
			"url": pr.html_url,
			"title": pr_title,
			"title_custom": pr_title not in subjects,
			"note": note,
			"head_branch": pr_branch,
			"base": pr_base,
			"commits": [{"sha": c.hexsha, "subject": subject_of(c)} for c in commits],
			"created": now_iso(),
			"updated": now_iso(),
			"state": "draft" if action == "created" else pr_state(pr),
		}
	)

	console.print(
		Panel(
			Group(f"[bold]#{pr.number}[/] {pr_title}", Text(pr.html_url, style=f"bold link {pr.html_url}")),
			title=f"[bold green]✓ PR {action}",
			title_align="left",
			border_style="green",
			expand=False,
		)
	)


if __name__ == "__main__":
	main()
