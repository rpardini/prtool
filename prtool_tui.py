"""prtool Textual TUI."""

import logging
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, Repo
from github import GithubException
from rich.table import Table
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.widgets import (
	Button,
	DataTable,
	Footer,
	Header,
	Input,
	Label,
	Markdown,
	RichLog,
	Select,
	SelectionList,
	Static,
	TextArea,
)
from textual.widgets.selection_list import Selection
from textual.worker import get_current_worker

import prtool_core as core

PR_STATE_STYLES = {"open": "green", "draft": "dim green", "merged": "magenta", "closed": "red", "unknown": "dim"}
NEW = "new"
OTHER = "__other__"
ADD = "__add__"


def esc(text: str) -> str:
	"""Escape for Textual markup (and Rich's). rich.markup.escape skips uppercase tags like '[NOPR]', which Textual parses."""
	return text.replace("[", "\\[")


def styled_state(state: str) -> str:
	return f"[{PR_STATE_STYLES.get(state, 'dim')}]{state}[/]"


@dataclass
class SendPlan:
	commits: list
	title: str
	body: str
	branch: str
	base: str
	note: list[str]
	existing_pr: object | None
	found_by: str  # "", "history", "branch"


# ---------------------------------------------------------------------------------------------------------------------
# Modal: generic confirmation


class ConfirmScreen(ModalScreen[bool]):
	BINDINGS = [Binding("escape,n", "answer(False)", "Cancel"), Binding("y", "answer(True)", "Yes")]

	def __init__(self, title: str, body, yes_label: str = "Yes"):
		super().__init__()
		self.title_text, self.body, self.yes_label = title, body, yes_label

	def compose(self) -> ComposeResult:
		with Vertical(classes="dialog"):
			yield Label(self.title_text, classes="dialog-title")
			yield Static(self.body)
			with Horizontal(classes="buttons"):
				yield Button(self.yes_label, variant="primary", id="yes")
				yield Button("Cancel", id="no")

	def on_mount(self):
		self.query_one("#yes", Button).focus()

	@on(Button.Pressed)
	def pressed(self, event: Button.Pressed):
		self.dismiss(event.button.id == "yes")

	def action_answer(self, answer: bool):
		self.dismiss(answer)


# ---------------------------------------------------------------------------------------------------------------------
# Modal: upstream moved; offer to rebase the work branch (flat history, never a merge)


class RebaseScreen(ModalScreen[bool]):
	BINDINGS = [Binding("escape", "answer(False)", "Not now")]
	MAX_SHOWN = 50

	def __init__(self, title: str, behind: list, conflicts: list[str]):
		super().__init__()
		self.title_text, self.behind, self.conflicts = title, behind, conflicts

	def compose(self) -> ComposeResult:
		lines = [f"[yellow]{c.hexsha[:10]}[/] {esc(core.subject_of(c))}" for c in self.behind[: self.MAX_SHOWN]]
		if len(self.behind) > self.MAX_SHOWN:
			lines.append(f"[dim]… and {len(self.behind) - self.MAX_SHOWN} more[/]")
		if self.conflicts:
			verdict = f"[red]✗ conflicts in:[/] {esc(', '.join(self.conflicts))}"
		else:
			verdict = "[green]✓ rebases cleanly[/] [dim](git merge-tree dry check)[/]"
		with Vertical(classes="dialog"):
			yield Label(self.title_text, classes="dialog-title")
			with VerticalScroll(id="behind-list"):
				yield Static("\n".join(lines))
			yield Static(verdict, id="rebase-verdict")
			with Horizontal(classes="buttons"):
				yield Button("Rebase if no conflicts", variant="primary", id="yes", disabled=bool(self.conflicts))
				yield Button("Not now", id="no")

	def on_mount(self):
		self.query_one("#no" if self.conflicts else "#yes", Button).focus()

	@on(Button.Pressed)
	def pressed(self, event: Button.Pressed):
		self.dismiss(event.button.id == "yes")

	def action_answer(self, answer: bool):
		self.dismiss(answer)


# ---------------------------------------------------------------------------------------------------------------------
# Modal: settings (upstream, base, push remote); suggestions are loaded in workers


class SettingsScreen(ModalScreen[dict | None]):
	BINDINGS = [Binding("escape", "cancel", "Cancel")]

	def __init__(self, first_run: bool):
		super().__init__()
		self.first_run = first_run

	def compose(self) -> ComposeResult:
		with Vertical(classes="dialog", id="settings-dialog"):
			yield Label(f"Settings for {self.app.clone_path}", classes="dialog-title")
			yield Label("Upstream repo (where PRs go)")
			yield Select([], id="upstream", prompt="Loading suggestions…")
			yield Input(placeholder="owner/repo, then Enter", id="upstream-other", classes="hidden")
			yield Label("Base branch for incoming PRs")
			yield Select([], id="base", prompt="Pick an upstream first")
			yield Input(placeholder="branch name", id="base-other", classes="hidden")
			yield Label("Push remote (your fork)")
			yield Select([], id="remote", prompt="Pick a remote")
			with Horizontal(id="add-remote", classes="hidden"):
				yield Input(placeholder="remote name", id="remote-name")
				yield Input(placeholder="git@github.com:you/repo.git", id="remote-url")
			yield Static("", id="settings-status")
			with Horizontal(classes="buttons"):
				yield Button("Save", variant="primary", id="save")
				yield Button("Use once", id="once", tooltip="Use for this session only; don't store")
				yield Button("Quit" if self.first_run else "Cancel", id="cancel")

	def on_mount(self):
		app: PrtoolApp = self.app
		current = app.cfg or {}
		remotes = core.push_remote_candidates(app.repo, app.login)
		options = [(f"{name}  {url}{'  (yours)' if mine else ''}", name) for name, url, mine in remotes]
		options.append(("Add a remote…", ADD))
		remote = self.query_one("#remote", Select)
		remote.set_options(options)
		names = [r[0] for r in remotes]
		remote.value = current.get("push_remote") if current.get("push_remote") in names else (remotes[0][0] if remotes and remotes[0][2] else ADD)
		self.query_one("#remote-name", Input).value = app.login
		self.load_upstreams(current.get("upstream"))

	def status(self, message: str):
		self.query_one("#settings-status", Static).update(message)

	@work(thread=True, exclusive=True, group="upstreams")
	def load_upstreams(self, current: str | None):
		app: PrtoolApp = self.app
		self.app.call_from_thread(self.status, "[dim]Inspecting remotes on GitHub…[/]")
		candidates = core.upstream_candidates(app.github, app.repo, current)
		self.app.call_from_thread(self.set_upstreams, candidates)

	def set_upstreams(self, candidates):
		select = self.query_one("#upstream", Select)
		select.set_options([(f"{slug}  [dim]{esc(why)}[/]", slug) for slug, why in candidates] + [("Other…", OTHER)])
		select.value = candidates[0][0] if candidates else OTHER
		self.status("")

	@on(Select.Changed, "#upstream")
	def upstream_changed(self, event: Select.Changed):
		other = event.value == OTHER
		self.query_one("#upstream-other", Input).set_class(not other, "hidden")
		if other:
			self.query_one("#upstream-other", Input).focus()
		elif event.value != Select.NULL:
			self.load_bases(event.value)

	@on(Input.Submitted, "#upstream-other")
	def upstream_other_submitted(self, event: Input.Submitted):
		if event.value.strip():
			self.load_bases(event.value.strip())

	@work(thread=True, exclusive=True, group="bases")
	def load_bases(self, slug: str):
		app: PrtoolApp = self.app
		self.app.call_from_thread(self.status, f"[dim]Looking at recent PRs on {slug}…[/]")
		try:
			gh_repo = app.github.get_repo(slug)
			current = (app.cfg or {}).get("base") if (app.cfg or {}).get("upstream", "").lower() == gh_repo.full_name.lower() else None
			rows, suggestion = core.base_candidates(gh_repo, current)
		except GithubException as e:
			self.app.call_from_thread(self.status, f"[red]Can't access '{slug}': {e.status}[/]")
			return
		if get_current_worker().is_cancelled:
			return
		self.app.call_from_thread(self.set_bases, gh_repo, rows, suggestion)

	def set_bases(self, gh_repo, rows, suggestion):
		select = self.query_one("#base", Select)
		options = []
		for branch, count, notes in rows:
			extra = f", {notes}" if notes else ""
			options.append((f"{branch}  [dim]({count} recent PRs{extra})[/]", branch))
		select.set_options(options + [("Other…", OTHER)])
		select.value = suggestion
		url = self.query_one("#remote-url", Input)
		if not url.value:
			url.value = f"git@github.com:{self.app.login}/{gh_repo.name}.git"
		self.status(f"[green]✓[/] {gh_repo.full_name}  [dim]default branch: {gh_repo.default_branch}[/]")

	@on(Select.Changed, "#base")
	def base_changed(self, event: Select.Changed):
		self.query_one("#base-other", Input).set_class(event.value != OTHER, "hidden")

	@on(Select.Changed, "#remote")
	def remote_changed(self, event: Select.Changed):
		self.query_one("#add-remote").set_class(event.value != ADD, "hidden")

	@on(Button.Pressed, "#cancel")
	def action_cancel(self):
		self.dismiss(None)

	@on(Button.Pressed, "#save,#once")
	def save(self, event: Button.Pressed):
		def value(select_id, other_id):
			v = self.query_one(select_id, Select).value
			return self.query_one(other_id, Input).value.strip() if v == OTHER else ("" if v == Select.NULL else v)

		upstream = value("#upstream", "#upstream-other")
		base = value("#base", "#base-other")
		remote = self.query_one("#remote", Select).value
		add_remote = None
		if remote == ADD:
			add_remote = (self.query_one("#remote-name", Input).value.strip(), self.query_one("#remote-url", Input).value.strip())
			remote = add_remote[0]
		if not upstream or not base or not remote or remote == Select.NULL or (add_remote and not add_remote[1]):
			self.status("[red]Fill in upstream, base and push remote.[/]")
			return
		self.validate_and_dismiss(upstream, base, remote, add_remote, event.button.id == "save")

	@work(thread=True, exclusive=True, group="save")
	def validate_and_dismiss(self, upstream, base, remote, add_remote, persist):
		app: PrtoolApp = self.app
		self.app.call_from_thread(self.status, "[dim]Validating…[/]")
		try:
			gh_repo = app.github.get_repo(upstream)
		except GithubException:
			self.app.call_from_thread(self.status, f"[red]Repo '{upstream}' not found / not accessible.[/]")
			return
		try:
			gh_repo.get_branch(base)
		except GithubException:
			self.app.call_from_thread(self.status, f"[red]Branch '{base}' doesn't exist in {gh_repo.full_name}.[/]")
			return
		if add_remote:
			try:
				app.repo.create_remote(*add_remote)
			except GitCommandError as e:
				self.app.call_from_thread(self.status, f"[red]Couldn't add remote: {esc(str(e))}[/]")
				return
			app.log_line(f"Added remote '{add_remote[0]}' → {add_remote[1]}")
		cfg = {"upstream": gh_repo.full_name, "base": base, "push_remote": remote, "configured_at": core.now_iso()}
		self.app.call_from_thread(self.dismiss, {"cfg": cfg, "persist": persist})


# ---------------------------------------------------------------------------------------------------------------------
# Screen: history of PRs sent to this upstream


class HistoryScreen(Screen[int | None]):
	BINDINGS = [
		Binding("escape", "back", "Back"),
		Binding("enter", "load", "Load into form", priority=True),
		Binding("o", "open", "Open in browser"),
		Binding("d", "drop", "Drop from history"),
		Binding("f5", "refresh_states", "Refresh"),
	]

	def compose(self) -> ComposeResult:
		yield Header()
		yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
		yield Footer()

	def on_mount(self):
		self.sub_title = f"PRs sent to {self.app.history.upstream}"
		table = self.query_one(DataTable)
		table.add_columns("PR", "State", "Title", "Branch → base", "Commits", "Updated")
		self.fill()
		self.action_refresh_states()

	def fill(self):
		table = self.query_one(DataTable)
		table.clear()
		for record in sorted(self.app.history.records, key=lambda r: r.get("updated", ""), reverse=True):
			title = esc(record["title"]) + (" [dim]✎[/]" if record.get("title_custom") else "")
			table.add_row(
				f"#{record['number']}",
				styled_state(record.get("state", "unknown")),
				title,
				f"[dim]{record['head_branch']} → {record['base']}[/]",
				str(len(record.get("commits", []))),
				record.get("updated", "")[:16].replace("T", " "),
				key=str(record["number"]),
			)

	@work(thread=True, exclusive=True)
	def action_refresh_states(self):
		app: PrtoolApp = self.app
		app.log_line("Refreshing PR states from GitHub…")
		for record in app.history.records:
			try:
				record["state"] = core.pr_state(app.upstream_repo.get_pull(record["number"]))
			except GithubException:
				record["state"] = "unknown"
		app.history.save()
		self.app.call_from_thread(self.fill)

	def current_number(self) -> int | None:
		table = self.query_one(DataTable)
		if not table.row_count:
			return None
		return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

	def action_back(self):
		self.dismiss(None)

	def action_load(self):
		self.dismiss(self.current_number())

	def action_open(self):
		number = self.current_number()
		if number is not None:
			webbrowser.open(self.app.history.get(number)["url"])

	def action_drop(self):
		number = self.current_number()
		if number is None:
			return

		def done(yes: bool):
			if yes:
				self.app.history.remove(number)
				self.fill()

		self.app.push_screen(ConfirmScreen(f"Drop #{number} from prtool's history?", "The PR itself is not touched.", "Drop"), done)


# ---------------------------------------------------------------------------------------------------------------------
# Main app


class SettingsBar(Static):
	def on_click(self):
		self.app.action_settings()


class CommitList(SelectionList):
	BINDINGS = [Binding("a", "select_all", "All"), Binding("n", "deselect_all", "None")]

	def action_select_all(self):
		# SelectionList.select_all() would also select disabled ([NOPR]) options
		with self.prevent(SelectionList.SelectedChanged):
			for option in list(self._options):
				if not option.disabled:
					self.select(option.value)
		self.post_message(SelectionList.SelectedChanged(self))

	def action_deselect_all(self):
		self.deselect_all()


class LogHandler(logging.Handler):
	def __init__(self, app: PrtoolApp):
		super().__init__()
		self.prtool_app = app

	def emit(self, record):
		self.prtool_app.log_line(f"[dim]{esc(self.format(record))}[/]")


class PrtoolApp(App):
	TITLE = "prtool"
	CSS = """
	#settings-bar { height: 1; background: $boost; padding: 0 1; }
	#warning { height: auto; background: $error 40%; padding: 0 1; display: none; }
	#warning.shown { display: block; }
	#main { height: 1fr; }
	#left { width: 45%; min-width: 40; }
	#left, #form, #preview-wrap, #log { border: round $primary 50%; }
	#left:focus-within, #form:focus-within, #preview-wrap:focus-within, #log:focus-within { border: round $accent; }
	#commits { height: 1fr; border: none; }
	#no-commits { color: $text-muted; padding: 1; display: none; }
	#no-commits.shown { display: block; }
	#right { width: 1fr; }
	#form { height: auto; padding: 0 1; }
	#form .row { height: 1; margin-top: 1; }
	#form #note-row { height: 6; margin: 1 0 1 0; }
	#form .row Label { width: 8; text-style: bold; color: $text-muted; }
	#form .row > Input, #form .row > Select, #form .row > TextArea { width: 1fr; }
	#note { height: 6; }
	#preview-wrap { height: 1fr; padding: 0 1; }
	#log { height: 9; }
	#log.hidden { display: none; }
	.hidden { display: none; }
	.dialog { width: 90; max-width: 95%; height: auto; max-height: 95%; padding: 1 2; background: $panel; border: thick $primary; }
	.dialog-title { text-style: bold; margin-bottom: 1; }
	.dialog Label { margin-top: 1; }
	.buttons { height: auto; margin-top: 1; align-horizontal: right; }
	.buttons Button { margin-left: 1; }
	#add-remote { height: auto; }
	#add-remote Input { width: 1fr; }
	ModalScreen { align: center middle; }
	#behind-list { height: auto; max-height: 20; }
	#rebase-verdict { margin-top: 1; }
	"""
	BINDINGS = [
		Binding("ctrl+s", "send", "Send PR", priority=True),
		Binding("f2", "settings", "Settings"),
		Binding("f3", "history", "History"),
		Binding("f5", "reload", "Fetch & reload"),
		Binding("f6", "offer_rebase", "Rebase…"),
		Binding("ctrl+t", "cycle_title", "Cycle title"),
		Binding("ctrl+o", "open_pr", "Open PR"),
		Binding("ctrl+l", "toggle_log", "Log"),
	]

	def __init__(self, args, repo: Repo):
		super().__init__()
		self.args = args
		self.repo = repo
		self.clone_path = str(Path(repo.working_tree_dir).resolve())
		self.github = None
		self.login = None
		self.cfg: dict | None = None
		self.cfg_persisted = True
		self.upstream_repo = None
		self.history: core.History | None = None
		self.work_branch = None
		self.problem: str | None = None
		self.commits: list = []
		self.first_load = True
		self.behind: list = []  # upstream commits not in the work branch, newest first
		self.dismissed_base_sha: str | None = None  # "Not now" for this upstream tip; don't re-ask until it moves
		self.base_override = args.base
		# form state
		self.target_pr = None  # live PR object when updating
		self.target_manual = False  # the user picked the target; don't auto-match over it
		self._target_value = NEW  # last value set programmatically; Select.Changed for it isn't the user
		self.title_dirty = False
		self.branch_dirty = False
		self.last_url = None
		self._title_cycle = 0
		self._preview_timer = None

	# --- layout ---

	def compose(self) -> ComposeResult:
		yield Header()
		yield SettingsBar("[dim]Logging in…[/]", id="settings-bar")
		yield Static("", id="warning")
		with Horizontal(id="main"):
			with Vertical(id="left"):
				yield CommitList(id="commits")
				yield Static("No commits ahead of the base branch.", id="no-commits")
			with Vertical(id="right"):
				with Vertical(id="form"):
					with Horizontal(classes="row"):
						yield Label("Target")
						yield Select([("New PR", NEW)], id="target", allow_blank=False, compact=True)
					with Horizontal(classes="row"):
						yield Label("Title")
						yield Input(id="title", placeholder="PR title (ctrl+t cycles through commit subjects)", compact=True)
					with Horizontal(classes="row"):
						yield Label("Branch")
						yield Input(id="branch", placeholder="pr/…", compact=True)
					with Horizontal(classes="row"):
						yield Label("Base")
						yield Input(id="base", placeholder="base branch", compact=True)
					with Horizontal(classes="row", id="note-row"):
						yield Label("Note")
						yield TextArea(id="note", language="markdown", soft_wrap=True, tab_behavior="indent", compact=True, placeholder="Optional note, shown as a quote atop the PR; one bullet per line")
				with VerticalScroll(id="preview-wrap"):
					yield Markdown("", id="preview")
		yield RichLog(id="log", markup=True, wrap=True)
		yield Footer()

	def on_mount(self):
		self.sub_title = self.clone_path
		self.query_one("#left").border_title = "Commits  [dim]space: toggle · a: all · n: none[/]"
		self.query_one("#form").border_title = "Pull request"
		self.query_one("#preview-wrap").border_title = "Body preview"
		self.query_one("#log").border_title = "Log"
		# all logging goes to the Log pane; stderr would garble the screen
		handler = LogHandler(self)
		handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
		logging.getLogger().addHandler(handler)
		self.startup()

	# --- logging, thread-safe ---

	def log_line(self, message: str):
		stamp = time.strftime("%H:%M:%S")
		line = f"[dim]{stamp}[/] {message}"
		if threading.current_thread() is threading.main_thread():
			self.query_one("#log", RichLog).write(line)
		else:
			self.call_from_thread(self.query_one("#log", RichLog).write, line)

	def log_plain(self, message: str):
		self.log_line(esc(message))

	def fail(self, message: str):
		self.log_line(f"[red]{esc(message)}[/]")
		if threading.current_thread() is threading.main_thread():
			self.notify(esc(message), severity="error", timeout=10)
		else:
			self.call_from_thread(self.notify, esc(message), severity="error", timeout=10)

	# --- startup / settings / reload ---

	@work(thread=True, exclusive=True, group="startup")
	def startup(self):
		self.log_line("Getting GitHub token from 'gh auth token'…")
		try:
			self.github, self.login = core.github_login()
		except core.PrtoolError as e:
			self.fail(str(e))
			return
		self.log_line(f"Logged in to GitHub as [b]{self.login}[/]")
		self.cfg = core.load_clone_config(self.clone_path)
		if self.cfg is None or self.args.reconfigure:
			self.call_from_thread(self.open_settings, True)
			return
		self.call_from_thread(self.reload)

	def action_settings(self):
		if self.github is None:
			self.notify("Still logging in to GitHub…")
			return
		self.open_settings(False)

	def open_settings(self, first_run: bool):
		def done(result):
			if result is None:
				if first_run:
					self.exit()
				return
			self.cfg = result["cfg"]
			self.cfg_persisted = result["persist"]
			self.base_override = None
			if result["persist"]:
				core.save_clone_config(self.clone_path, self.cfg)
				self.log_line(f"Saved settings to {core.clones_file()}")
			self.reload(fetch=True)

		self.push_screen(SettingsScreen(first_run), done)

	def action_reload(self):
		if self.cfg is not None:
			self.reload(fetch=True)

	@property
	def base(self) -> str:
		return self.base_override or self.cfg["base"]

	@property
	def push_remote(self) -> str:
		return self.cfg["push_remote"]

	@property
	def owner(self) -> str:
		return core.remote_owner(self.repo, self.push_remote, self.login)

	@work(thread=True, exclusive=True, group="reload")
	def reload(self, fetch: bool | None = None):
		self.call_from_thread(setattr, self.query_one("#left"), "loading", True)
		try:
			self._reload(self.args.fetch if fetch is None else fetch)
		except (core.PrtoolError, GitCommandError, GithubException) as e:
			self.fail(str(e))
		finally:
			self.call_from_thread(setattr, self.query_one("#left"), "loading", False)

	def _reload(self, fetch: bool):
		if self.push_remote not in [r.name for r in self.repo.remotes]:
			raise core.PrtoolError(f"Push remote '{self.push_remote}' doesn't exist anymore; fix it in Settings (F2).")
		if self.upstream_repo is None or self.upstream_repo.full_name.lower() != self.cfg["upstream"].lower():
			self.upstream_repo = self.github.get_repo(self.cfg["upstream"])
			self.history = core.History(self.upstream_repo.full_name)
		self.problem = core.working_copy_problem(self.repo)
		self.work_branch = None if self.repo.head.is_detached else self.repo.active_branch.name
		self.call_from_thread(self.update_bars)

		core.ensure_base(self.repo, self.upstream_repo.full_name, self.base, fetch, self.log_plain)
		base_ref = core.upstream_ref(self.base)
		self.behind = core.commits_behind(self.repo, base_ref)
		if self.behind:
			self.log_line(f"[yellow]{core.upstream_ref(self.base)} is {len(self.behind)} commit{'s' if len(self.behind) != 1 else ''} ahead of '{esc(self.work_branch or 'HEAD')}'[/]")
		commits = core.commits_ahead(self.repo, base_ref)

		preselect = None
		if self.first_load and self.args.revisions:
			try:
				preselect = {c.hexsha for c in core.resolve_revisions(self.repo, self.args.revisions)}
			except (GitCommandError, ValueError) as e:
				self.fail(f"Can't resolve revisions {self.args.revisions}: {e}")
		self.log_line(f"{len(commits)} commits ahead of {base_ref}")
		self.call_from_thread(self.populate, commits, preselect)
		if self.first_load and self.args.list:
			self.call_from_thread(self.action_history)
		elif self.args.rebase and self.behind and self.behind[0].hexsha != self.dismissed_base_sha:
			self._offer_rebase()
		self.first_load = False

	def update_bars(self):
		upstream = self.upstream_repo.full_name if self.upstream_repo else self.cfg["upstream"]
		session = "" if self.cfg_persisted and not self.base_override else "  [yellow](session only)[/]"
		behind = f" [yellow]+{len(self.behind)}[/]" if self.behind else ""
		self.query_one("#settings-bar", SettingsBar).update(
			f"⚙ [b]{upstream}[/] ← [b]{self.owner}[/]   base [b]{self.base}[/]{behind}   push [b]{self.push_remote}[/]   "
			f"work [b]{self.work_branch or '(detached)'}[/]{session}   [dim]F2 / click to edit[/]"
		)
		warning = self.query_one("#warning", Static)
		warning.update(f"⚠ {self.problem}  [dim]Sending is disabled; F5 re-checks.[/]" if self.problem else "")
		warning.set_class(bool(self.problem), "shown")

	def populate(self, commits: list, preselect: set | None):
		commit_list = self.query_one("#commits", CommitList)
		if preselect is None:
			# keep the selection across reloads by subject (rebases change SHAs)
			selected = {core.subject_of(c) for c in self.selected_commits()}
			preselect = {c.hexsha for c in commits if core.subject_of(c) in selected}
		self.commits = commits
		by_subject = self.history.pr_by_subject()
		with commit_list.prevent(SelectionList.SelectedChanged):
			commit_list.clear_options()
			for commit in commits:
				subject = core.subject_of(commit)
				if core.is_nopr(commit):
					prompt = f"[dim]{commit.hexsha[:10]} {esc(subject)}  (not for PRs)[/]"
					commit_list.add_option(Selection(prompt, commit.hexsha, False, disabled=True))
					continue
				record = by_subject.get(subject)
				badge = f"  [b]#{record['number']}[/] {styled_state(record.get('state', 'unknown'))}" if record else ""
				prompt = f"[yellow]{commit.hexsha[:10]}[/] {esc(subject)}{badge}"
				commit_list.add_option(Selection(prompt, commit.hexsha, commit.hexsha in preselect))
		self.query_one("#no-commits").set_class(not commits, "shown")
		self.query_one("#title", Input).suggester = SuggestFromList([core.subject_of(c) for c in commits], case_sensitive=False)
		self.refresh_targets()
		self.update_bars()
		if not self.query_one("#base", Input).value or self.target_pr is None:
			self.set_input("#base", self.base)
		self.selection_changed()
		commit_list.focus()

	def refresh_targets(self):
		select = self.query_one("#target", Select)
		options = [("New PR", NEW)]
		for record in sorted(self.history.open_records(), key=lambda r: r.get("updated", ""), reverse=True):
			options.append((f"Update #{record['number']}  {esc(record['title'])}", record["number"]))
		if self.target_pr is not None and self.target_pr.number not in [o[1] for o in options]:
			options.append((f"Update #{self.target_pr.number}  {esc(self.target_pr.title)}", self.target_pr.number))
		current = select.value
		with select.prevent(Select.Changed):
			select.set_options(options)
			select.value = self._target_value = current if current in [o[1] for o in options] else NEW

	# --- form ---

	def selected_commits(self) -> list:
		selected = set(self.query_one("#commits", CommitList).selected)
		return [c for c in self.commits if c.hexsha in selected and not core.is_nopr(c)]

	def set_input(self, selector: str, value: str):
		widget = self.query_one(selector, Input)
		with widget.prevent(Input.Changed):
			widget.value = value
		self.schedule_preview()

	@on(SelectionList.SelectedChanged, "#commits")
	def selection_changed(self):
		subjects = [core.subject_of(c) for c in self.selected_commits()]
		if not self.target_manual:
			self.match_history(subjects)
		if self.target_pr is None and not self.title_dirty:
			self.set_input("#title", subjects[0] if subjects else "")
			self.auto_branch()
		self.schedule_preview()

	@work(thread=True, exclusive=True, group="match")
	def match_history(self, subjects: list[str]):
		time.sleep(0.3)  # debounce
		if get_current_worker().is_cancelled or not subjects or self.history is None:
			if not subjects:
				self.call_from_thread(self.apply_match, None)
			return
		try:
			match = core.find_history_match(self.history, self.upstream_repo, subjects)
		except GithubException as e:
			self.fail(f"GitHub error while matching history: {e}")
			return
		if not get_current_worker().is_cancelled:
			self.call_from_thread(self.apply_match, match, len(subjects))

	def apply_match(self, match, total: int = 0):
		if self.target_manual:
			return
		if match:
			record, pr, overlap = match
			if self.target_pr is None or self.target_pr.number != pr.number:
				self.log_line(f"Selected commits match [b]#{pr.number}[/] ({overlap}/{total}): {esc(record['title'])}")
				self.apply_target(pr, record)
		elif self.target_pr is not None:
			self.apply_target(None, None)

	def apply_target(self, pr, record: dict | None):
		"""Fill the form from an existing PR (+ its history record), or reset it to 'New PR'."""
		self.target_pr = pr
		self.refresh_targets()
		select = self.query_one("#target", Select)
		with select.prevent(Select.Changed):
			select.value = self._target_value = pr.number if pr is not None else NEW
		branch = self.query_one("#branch", Input)
		note = self.query_one("#note", TextArea)
		self.title_dirty = False
		self.branch_dirty = False
		if pr is not None:
			self.set_input("#title", record["title"] if record else pr.title)
			self.set_input("#branch", pr.head.ref)
			self.set_input("#base", pr.base.ref)
			note.text = "\n".join(record.get("note", []) if record else [])
			branch.disabled = True
		else:
			subjects = [core.subject_of(c) for c in self.selected_commits()]
			self.set_input("#title", subjects[0] if subjects else "")
			self.set_input("#base", self.base)
			note.text = ""
			branch.disabled = False
			self.auto_branch()
		self.schedule_preview()

	@on(Select.Changed, "#target")
	def target_changed(self, event: Select.Changed):
		if event.value == self._target_value:
			return  # initial mount / programmatic change, not the user
		self._target_value = event.value
		self.target_manual = True
		if event.value == NEW:
			self.target_pr = None
			self.query_one("#branch", Input).disabled = False
			self.branch_dirty = False
			self.auto_branch()
			self.log_line("Target: new PR (auto-matching off until next reload)")
		else:
			self.load_target(event.value)

	@work(thread=True, exclusive=True, group="target")
	def load_target(self, number: int):
		try:
			pr = self.upstream_repo.get_pull(number)
		except GithubException as e:
			self.fail(f"Can't load PR #{number}: {e}")
			return
		self.call_from_thread(self.apply_target, pr, self.history.get(number))

	@on(Input.Changed, "#title")
	def title_changed(self):
		self.title_dirty = True
		self.auto_branch()
		self.schedule_preview()

	@on(Input.Changed, "#branch")
	def branch_changed(self):
		self.branch_dirty = True

	@on(Input.Changed, "#base")
	@on(TextArea.Changed, "#note")
	def other_changed(self):
		self.schedule_preview()

	def auto_branch(self):
		if self.target_pr is None and not self.branch_dirty:
			title = self.query_one("#title", Input).value
			self.set_input("#branch", core.normalize_branch(title) if title else "")

	def action_cycle_title(self):
		subjects = [core.subject_of(c) for c in self.selected_commits()] or [core.subject_of(c) for c in self.commits]
		if subjects:
			self._title_cycle = (self._title_cycle + 1) % len(subjects)
			self.set_input("#title", subjects[self._title_cycle])
			self.title_dirty = True
			self.auto_branch()

	def current_note(self) -> list[str]:
		return core.normalize_note(self.query_one("#note", TextArea).text)

	def schedule_preview(self):
		if self._preview_timer is not None:
			self._preview_timer.stop()
		self._preview_timer = self.set_timer(0.15, self.update_preview)

	def update_preview(self):
		commits = self.selected_commits()
		preview = self.query_one("#preview", Markdown)
		if not commits:
			preview.update("*Select some commits on the left.*")
			return
		title = self.query_one("#title", Input).value.strip() or "(no title)"
		self.query_one("#preview-wrap").border_title = f"Body preview · [b]{esc(title)}[/]"
		preview.update(core.build_body(title, [c.message for c in commits], self.current_note()))

	# --- sending ---

	def action_send(self):
		if self.cfg is None or self.history is None:
			self.notify("Not ready yet.")
			return
		if self.problem:
			self.notify(self.problem, severity="error")
			return
		commits = self.selected_commits()
		title = self.query_one("#title", Input).value.strip()
		branch = self.query_one("#branch", Input).value.strip()
		base = self.query_one("#base", Input).value.strip()
		missing = [n for n, v in (("commits", commits), ("title", title), ("branch", branch), ("base", base)) if not v]
		if missing:
			self.notify(f"Missing: {', '.join(missing)}", severity="warning")
			return
		note = self.current_note()
		plan = SendPlan(commits, title, core.build_body(title, [c.message for c in commits], note), branch, base, note, self.target_pr, "history" if self.target_pr else "")
		self.prepare_send(plan)

	@work(thread=True, exclusive=True, group="send")
	def prepare_send(self, plan: SendPlan):
		try:
			if plan.existing_pr is None:
				plan.existing_pr = core.find_open_pr_by_branch(self.upstream_repo, self.owner, plan.branch)
				if plan.existing_pr is not None:
					plan.found_by = "branch"
					self.log_line(f"Found open PR [b]#{plan.existing_pr.number}[/] with the same branch; will update it")
			if plan.existing_pr is not None and plan.existing_pr.base.ref != plan.base:
				self.log_line(f"[yellow]Note: base change {plan.existing_pr.base.ref} → {plan.base} isn't applied to existing PRs; using {plan.existing_pr.base.ref}[/]")
				plan.base = plan.existing_pr.base.ref
			core.ensure_base(self.repo, self.upstream_repo.full_name, plan.base, plan.base != self.base, self.log_plain)
		except (GithubException, GitCommandError, core.PrtoolError) as e:
			self.fail(str(e))
			return
		self.call_from_thread(self.confirm_send, plan)

	def confirm_send(self, plan: SendPlan):
		grid = Table.grid(padding=(0, 2))
		grid.add_column(style="bold cyan", no_wrap=True)
		grid.add_column()
		grid.add_row("Upstream", f"{self.upstream_repo.full_name} ← {self.owner}:{plan.branch}")
		grid.add_row("Base", f"{plan.base} ({core.upstream_ref(plan.base)})")
		if plan.existing_pr is not None:
			how = " [dim](found by branch name)[/]" if plan.found_by == "branch" else ""
			grid.add_row("Action", f"[yellow]update[/] #{plan.existing_pr.number}{how}\n{plan.existing_pr.html_url}")
		else:
			grid.add_row("Action", "[green]create draft PR[/]")
		grid.add_row("Title", f"[bold]{esc(plan.title)}[/]")
		grid.add_row("Commits", "\n".join(f"[yellow]{c.hexsha[:10]}[/] {esc(core.subject_of(c))}" for c in plan.commits))
		grid.add_row("Push", f"force-push to '{self.push_remote}'")

		def done(yes: bool):
			if yes:
				self.do_send(plan)

		self.push_screen(ConfirmScreen("Send this PR?", grid, "Send"), done)

	@work(thread=True, exclusive=True, group="send")
	def do_send(self, plan: SendPlan):
		try:
			shas = [c.hexsha for c in plan.commits]
			core.push_pr_branch(self.repo, self.work_branch, plan.branch, core.upstream_ref(plan.base), shas, self.push_remote, self.log_plain)
			self.log_line("Talking to GitHub…")
			pr, action = core.send_pr(self.upstream_repo, self.history, plan.existing_pr, self.owner, plan.title, plan.body, plan.branch, plan.base, plan.note, plan.commits)
		except GitCommandError as e:
			self.fail(f"Git failed; back on '{self.work_branch}'. {e}")
			return
		except GithubException as e:
			self.fail(f"GitHub failed: {e}")
			return
		self.last_url = pr.html_url
		self.log_line(f"[bold green]✓ PR {action}[/]: #{pr.number} {pr.html_url}")
		self.call_from_thread(self.notify, f"#{pr.number} {pr.html_url}\nctrl+o opens it", title=f"PR {action}", timeout=15)
		self.call_from_thread(self.after_send, pr)

	def after_send(self, pr):
		self.target_pr = pr
		self.populate(self.commits, None)
		select = self.query_one("#target", Select)
		with select.prevent(Select.Changed):
			select.value = self._target_value = pr.number
		self.query_one("#branch", Input).disabled = True

	# --- rebasing the work branch onto upstream (never merging) ---

	def action_offer_rebase(self):
		if self.cfg is None or self.upstream_repo is None:
			self.notify("Not ready yet.")
			return
		if self.problem:
			self.notify(self.problem, severity="error")
			return
		if not self.behind:
			self.notify(f"'{self.work_branch}' is up to date with {self.upstream_repo.full_name} '{self.base}'.")
			return
		self.offer_rebase_worker()

	@work(thread=True, exclusive=True, group="rebase")
	def offer_rebase_worker(self):
		self._offer_rebase()

	def _offer_rebase(self):
		"""Worker thread: dry-check with merge-tree, then ask."""
		if self.problem or not self.behind:
			return
		base_ref = core.upstream_ref(self.base)
		try:
			conflicts = core.rebase_conflicts(self.repo, base_ref)
		except core.PrtoolError as e:
			self.fail(str(e))
			return
		self.call_from_thread(self.ask_rebase, list(self.behind), conflicts)

	def ask_rebase(self, behind: list, conflicts: list[str]):
		def done(yes: bool):
			if yes:
				self.rebase_now()
			else:
				self.dismissed_base_sha = behind[0].hexsha
				self.log_line("Not rebasing; F6 offers it again")

		title = f"{self.upstream_repo.full_name} '{self.base}' is {len(behind)} commit{'s' if len(behind) != 1 else ''} ahead of '{self.work_branch}'"
		self.push_screen(RebaseScreen(esc(title), behind, conflicts), done)

	@work(thread=True, exclusive=True, group="rebase")
	def rebase_now(self):
		base_ref = core.upstream_ref(self.base)
		try:
			if core.working_copy_problem(self.repo):
				raise core.PrtoolError(core.working_copy_problem(self.repo))
			conflicts = core.rebase_conflicts(self.repo, base_ref)
			if conflicts:
				raise core.PrtoolError(f"Not rebasing; conflicts in: {', '.join(conflicts)}")
			core.rebase_work_branch(self.repo, self.work_branch, base_ref, self.log_plain)
		except (core.PrtoolError, GitCommandError) as e:
			self.fail(str(e))
			return
		self.call_from_thread(self.reload, False)

	# --- other actions ---

	def action_history(self):
		if self.history is None:
			self.notify("Not ready yet.")
			return

		def done(number: int | None):
			self.populate(self.commits, None)
			if number is None:
				return
			record = self.history.get(number)
			subjects = {c["subject"] for c in record.get("commits", [])}
			commit_list = self.query_one("#commits", CommitList)
			with commit_list.prevent(SelectionList.SelectedChanged):
				commit_list.deselect_all()
				for commit in self.commits:
					if core.subject_of(commit) in subjects:
						commit_list.select(commit.hexsha)
			self.target_manual = True
			self.load_target(number)
			if not any(core.subject_of(c) in subjects for c in self.commits):
				self.notify(f"None of #{number}'s commits are ahead of the base anymore (merged?).", severity="warning")

		self.push_screen(HistoryScreen(), done)

	def action_open_pr(self):
		url = self.target_pr.html_url if self.target_pr is not None else self.last_url
		if url:
			webbrowser.open(url)
		else:
			self.notify("No PR selected yet.")

	def action_toggle_log(self):
		self.query_one("#log").toggle_class("hidden")
