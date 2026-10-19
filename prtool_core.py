"""prtool core: config/history storage, git & GitHub helpers, PR body Markdown. No UI in here."""

import logging
import os
import random
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from itertools import islice
from pathlib import Path

import yaml
from git import GitCommandError, Repo
from github import Auth, Github, GithubException

logger = logging.getLogger("prtool")

LogFn = Callable[[str], None]


class PrtoolError(Exception):
	pass


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

	def remove(self, number: int):
		self.records = [r for r in self.records if r.get("number") != number]
		self.save()

	def open_records(self) -> list[dict]:
		return [r for r in self.records if r.get("state") not in ("merged", "closed")]

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
	try:
		out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout
	except FileNotFoundError as e:
		raise PrtoolError("'gh' (GitHub CLI) not found in PATH.") from e
	except subprocess.CalledProcessError as e:
		raise PrtoolError(f"'gh auth token' failed; run 'gh auth login' first. {e.stderr.strip()}") from e
	github = Github(auth=Auth.Token(out.strip().splitlines()[0]))
	return github, github.get_user().login


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


def remote_owner(repo: Repo, remote_name: str, fallback: str) -> str:
	return (parse_github_slug(repo.remotes[remote_name].url) or fallback).split("/")[0]


# ---------------------------------------------------------------------------------------------------------------------
# Settings suggestions


def upstream_candidates(github: Github, repo: Repo, current: str | None) -> list[tuple[str, str]]:
	"""[(owner/repo, why)], most likely first."""
	candidates: list[tuple[str, str]] = []

	def add(slug, why):
		if slug and slug.lower() not in [c[0].lower() for c in candidates]:
			candidates.append((slug, why))

	if current:
		add(current, "current setting")

	remote_repos = []
	for remote in repo.remotes:
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
	return candidates


def base_candidates(upstream_repo, current: str | None) -> tuple[list[tuple[str, int, str]], str]:
	"""([(branch, recent PR count, notes)], suggestion), from the default branch and the bases of recent PRs."""
	counts = Counter()
	for pr in islice(upstream_repo.get_pulls(state="all", sort="created", direction="desc"), 30):
		counts[pr.base.ref] += 1
	default_branch = upstream_repo.default_branch
	branches = [b for b, _ in counts.most_common()]
	if default_branch not in branches:
		branches.append(default_branch)
	if current and current not in branches:
		branches.append(current)
	rows = []
	for branch in branches:
		notes = []
		if branch == default_branch:
			notes.append("default")
		if branch == current:
			notes.append("current")
		rows.append((branch, counts.get(branch, 0), ", ".join(notes)))
	suggestion = current or (counts.most_common(1)[0][0] if counts else default_branch)
	return rows, suggestion


def push_remote_candidates(repo: Repo, login: str) -> list[tuple[str, str, bool]]:
	"""[(remote name, url, is it yours)], yours first."""
	rows = []
	for remote in repo.remotes:
		slug = parse_github_slug(remote.url)
		rows.append((remote.name, remote.url, bool(slug) and slug.split("/")[0].lower() == login.lower()))
	return sorted(rows, key=lambda r: not r[2])


# ---------------------------------------------------------------------------------------------------------------------
# Git helpers


def upstream_ref(base: str) -> str:
	return f"refs/prtool/upstream/{base}"


def ref_exists(repo: Repo, ref: str) -> bool:
	try:
		repo.git.rev_parse("--verify", "-q", ref)
		return True
	except GitCommandError:
		return False


def fetch_base(repo: Repo, upstream: str, base: str, log: LogFn):
	# Fetch by URL into a private ref, so no correctly-named/pointed 'upstream' remote is needed.
	# Prefer the URL of a local remote pointing at upstream (keeps its auth method), otherwise https.
	url = next(
		(r.url for r in repo.remotes if (parse_github_slug(r.url) or "").lower() == upstream.lower()),
		f"https://github.com/{upstream}.git",
	)
	log(f"Fetching {upstream} '{base}' from {url}...")
	repo.git.fetch(url, f"+refs/heads/{base}:{upstream_ref(base)}")
	log(f"Fetched {upstream} '{base}' → {upstream_ref(base)}")


def ensure_base(repo: Repo, upstream: str, base: str, fetch: bool, log: LogFn):
	if fetch or not ref_exists(repo, upstream_ref(base)):
		fetch_base(repo, upstream, base, log)


def working_copy_problem(repo: Repo) -> str | None:
	if repo.head.is_detached:
		return "HEAD is detached; checkout your work branch first."
	if repo.is_dirty():
		return "Working copy is dirty; commit or stash your changes first."
	return None


def rebase_work_branch(repo: Repo, work_branch: str, base_ref: str, log: LogFn):
	# Rebase the work branch on top of the upstream branch, for sanity; revert cleanly on conflicts.
	log(f"Rebasing '{work_branch}' onto {base_ref}...")
	try:
		repo.git.rebase(base_ref, work_branch)
	except GitCommandError as e:
		try:
			repo.git.rebase("--abort")
		except GitCommandError:
			pass
		raise PrtoolError(f"Rebase of '{work_branch}' onto {base_ref} failed and was aborted; resolve manually.\n{e.stderr}") from e
	log(f"Rebased '{work_branch}' onto {base_ref}")


def resolve_revisions(repo: Repo, revisions: list[str]) -> list:
	commits = []
	for revision in revisions:
		if ".." in revision:
			commits.extend(reversed(list(repo.iter_commits(revision))))  # oldest first
		else:
			commits.append(repo.commit(revision))
	seen = set()
	return [c for c in commits if not (c.hexsha in seen or seen.add(c.hexsha))]


def commits_ahead(repo: Repo, base_ref: str) -> list:
	return list(reversed(list(repo.iter_commits(f"{base_ref}..HEAD", no_merges=True))))  # oldest first


def subject_of(commit) -> str:
	return commit.message.splitlines()[0].strip()


def push_pr_branch(repo: Repo, work_branch: str, pr_branch: str, base_ref: str, shas: list[str], push_remote: str, log: LogFn):
	if pr_branch == work_branch:
		raise PrtoolError(f"PR branch '{pr_branch}' is the work branch itself.")
	try:
		log(f"Creating '{pr_branch}' from {base_ref}...")
		repo.git.checkout("-B", pr_branch, base_ref)
		for sha in shas:
			log(f"Cherry-picking {sha[:10]}...")
			repo.git.cherry_pick(sha)
		log(f"Force-pushing '{pr_branch}' to '{push_remote}'...")
		repo.git.push("-f", push_remote, pr_branch)
		log(f"Pushed '{pr_branch}' to '{push_remote}'")
	except GitCommandError:
		try:
			repo.git.cherry_pick("--abort")
		except GitCommandError:
			pass
		raise
	finally:
		repo.git.checkout(work_branch)


# ---------------------------------------------------------------------------------------------------------------------
# Finding & sending PRs


def find_history_match(history: History, upstream_repo, subjects: list[str]):
	"""Find an open, previously-sent PR sharing commit subjects (they survive rebases/amends, SHAs don't).
	Returns (record, live pr, overlap) or None."""
	scored = []
	for record in history.open_records():
		overlap = len(set(subjects) & {c["subject"] for c in record.get("commits", [])})
		if overlap:
			scored.append((overlap, record.get("updated", ""), record))
	scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
	for overlap, _, record in scored:
		try:
			pr = upstream_repo.get_pull(record["number"])
		except GithubException:
			continue
		record["state"] = pr_state(pr)
		history.save()
		if record["state"] in ("open", "draft"):
			return record, pr, overlap
	return None


def find_open_pr_by_branch(upstream_repo, owner: str, branch: str):
	return next(iter(upstream_repo.get_pulls(state="open", head=f"{owner}:{branch}")), None)


def normalize_branch(title: str) -> str:
	# Replace spaces with dashes, and remove any non-alphanumeric characters.
	normalized = "".join(c for c in title.replace(" ", "-") if c.isalnum() or c == "-")
	return "pr/" + normalized


def send_pr(upstream_repo, history: History, existing_pr, owner: str, title: str, body: str, branch: str, base: str, note: list[str], commits: list):
	"""Create (draft) or update the PR, and record it in history. Returns (pr, 'created' | 'updated')."""
	if existing_pr is not None:
		existing_pr.edit(title=title, body=body)
		pr, action = existing_pr, "updated"
	else:
		pr = upstream_repo.create_pull(
			maintainer_can_modify=False,  # dont leak my secrets
			title=title,
			body=body,
			head=f"{owner}:{branch}",
			base=base,
			draft=True,
		)
		action = "created"
	subjects = [subject_of(c) for c in commits]
	history.upsert(
		{
			"number": pr.number,
			"url": pr.html_url,
			"title": title,
			"title_custom": title not in subjects,
			"note": note,
			"head_branch": branch,
			"base": base,
			"commits": [{"sha": c.hexsha, "subject": subject_of(c)} for c in commits],
			"created": now_iso(),
			"updated": now_iso(),
			"state": "draft" if action == "created" else pr_state(pr),
		}
	)
	return pr, action


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
		if stripped_line.startswith(("Signed-off-by:", "Assisted-by:", "Reviewed-by:", "Co-authored-by:")):
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


# PR note: optional Markdown bullets, rendered as a blockquote atop the PR body


def normalize_note(text: str) -> list[str]:
	# each unindented line is a bullet; existing bullets (incl. nested) and indented continuation lines are kept as-is
	lines = []
	for line in text.splitlines():
		if not line.strip():
			continue
		if line.lstrip().startswith("- ") or (lines and line[0] in " \t"):
			lines.append(line.rstrip())
		else:
			lines.append(f"- {line.strip()}")
	return lines


def render_note(note: list[str]) -> str:
	if not note:
		return ""
	return "\n".join(f"> {line}" for line in rewrap_lines("\n".join(note)).splitlines())
