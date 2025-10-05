import logging
import os
import sys
from pathlib import Path

from git import Repo
from github import Github, Auth
from rich.logging import RichHandler

logging.basicConfig(
	level=logging.INFO,
	format="%(message)s",
	datefmt="[%X]",
	handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger("prtool")

# Using PyGithub, so initialize it, and make sure we've a valid token, from ghcli.
logger.info(f"Running prtool in {Path.cwd()}")

# run "gh auth token" and get the token from the output
logger.info("Getting GitHub token from ghcli... 'gh auth token' must work...")

# run the command
gh_auth_token_command = "gh auth token"
gh_auth_token_output = os.popen(gh_auth_token_command).read()
# split the output by newline, and get the first line
token = gh_auth_token_output.splitlines()[0]
logger.info(f"Got GitHub token from ghcli: '{token[1:5]}....'")

# Initialize PyGithub with the token.
# using an access token
gh_auth_token = Auth.Token(token)
github = Github(auth=gh_auth_token)

# Get the current user, to make sure the token is valid.
github_user = github.get_user()
gh_login_from_token = github_user.login
logger.info(f"Logged in as '{gh_login_from_token}'")

# Some basic definitions.

# git_workdir is the current directory
current_directory = os.getcwd()
# make sure it contains a .git directory
if not os.path.exists(f"{current_directory}/.git"):
	logger.error(f"Directory '{current_directory}' doesn't contain a .git directory.")
	sys.exit(1)
git_workdir = current_directory

should_fetch = True
should_rebase = True

upstream_remote_name = "upstream"
upstream_branch_name = "main"

pr_to_remote_name = gh_login_from_token  # automatic from login token, thus always 'rpardini'
upstream_reference = upstream_remote_name + "/" + upstream_branch_name

logger.info(f"Getting revisions to PR...")
# get a space-delimited list of revisions to PR, from the arguments passed to the script
revisions_to_pr = " ".join(sys.argv[1:])
logger.info(f"Revisions to PR: {revisions_to_pr}")

# Create a GitPython repo on the git_workdir directory
git_repo = Repo(git_workdir)

# Get the current branch name
current_branch_name = git_repo.active_branch.name
logger.info(f"Current branch name: '{current_branch_name}'")
work_branch_name = current_branch_name

# Find the PyGitHub repo, from the GitPython repo's remotes.
github_upstream_repo = None
for remote in git_repo.remotes:
	logger.debug(f"Testing remote: '{remote.name}' with url '{remote.url}'...")
	if remote.name == upstream_remote_name:
		github_org_repo = remote.url.split(":")[1]
		# remove ".git" from the end, if it's there
		if github_org_repo.endswith(".git"):
			github_org_repo = github_org_repo[:-4]
		github_upstream_repo = github.get_repo(github_org_repo)
		logger.info(f"Found GitHub repo '{github_upstream_repo.full_name}'")
		break

if github_upstream_repo is None:
	logger.error(f"GitHub repo not found.")
	sys.exit(1)

# Make sure we've a clean working copy, no pending changes are allowed.
if git_repo.is_dirty():
	logger.error(
		f"Working copy is dirty. Please commit or stash your changes. Rebase against the upstream branch '{upstream_reference}' first and resolve any conflicts. Squash fixups and amend commits as needed."
	)
	sys.exit(1)

# Checkout the work branch, so we're clean to continue working.
logger.info(f"Checking out work branch '{work_branch_name}' initial...")
git_repo.git.checkout(work_branch_name)

# Fetch the upstream branch
if should_fetch:
	logger.info(f"Fetching upstream branch '{upstream_branch_name}'...")
	git_repo.remotes[upstream_remote_name].fetch(upstream_branch_name)

# Rebase the work branch on top of the upstream branch # WHY? for sanity, but should fail cleanly and revert to work branch on conflicts
if should_rebase:
	logger.info(f"Rebasing work branch '{work_branch_name}' on top of upstream branch '{upstream_reference}'...")
	git_repo.git.rebase(upstream_reference, work_branch_name)

# Checkout the work branch, so we're clean to continue working.
logger.info(f"Checking out work branch '{work_branch_name}' again ...")
git_repo.git.checkout(work_branch_name)

# Loop over the commits to be PR'ed. Accumulate the commit messages in a list.
commit_messages = []
cherry_pick_sha1s = []

# if revisions_to_pr contains a "…" then it's a range of commits, use iter_commits
# otherwise it's a list of commits, use iter_rev
# first split by space
if ".." not in revisions_to_pr:
	revisions_to_pr_list = revisions_to_pr.split(" ")
	for revision in revisions_to_pr_list:
		commit = git_repo.commit(revision)
		commit_messages.append(commit.message)
		cherry_pick_sha1s.append(commit.hexsha)
else:
	for commit in git_repo.iter_commits(revisions_to_pr):
		commit_messages.append(commit.message)
		cherry_pick_sha1s.append(commit.hexsha)

for sha1 in cherry_pick_sha1s:
	# make sure the sha1 is in the work branch, not a floating/dangling commit
	if not git_repo.git.branch("--contains", sha1).find(work_branch_name) >= 0:
		logger.error(f"Commit '{sha1}' not found in work branch '{work_branch_name}'.")
		sys.exit(1)

# If more than one commit, ask the user, which commit to use as title.
pr_title = None
if len(commit_messages) > 1:
	# Show a list of commits, and ask the user to select one, or type a completely new one.
	commit_messages_with_index = []
	for index, commit_message in enumerate(commit_messages):
		commit_messages_with_index.append(f"{index + 1}. {commit_message.splitlines()[0]}")
	commit_messages_with_index.append("n. Type a new title")
	commit_messages_with_index.append("q. Quit")
	commit_messages_with_index_str = "\n".join(commit_messages_with_index)
	commit_messages_with_index_str += "\n\nEnter your choice: "
	choice = input(commit_messages_with_index_str)
	if choice == "q":
		logger.info(f"Aborting.")
		sys.exit(0)
	elif choice == "n":
		pr_title = input("Enter the PR title: ")
	else:
		choice_int = int(choice)
		if choice_int < 1 or choice_int > len(commit_messages):
			logger.error(f"Invalid choice '{choice}'.")
			sys.exit(1)
		# Subtract 1, since the list is 0-based, but the choices are 1-based.
		pr_title = commit_messages[choice_int - 1].splitlines()[0].strip()
else:
	# Create the suggested PR title, from the first commit's first line
	pr_title = commit_messages[0].splitlines()[0].strip()

markdown_commit_messages = []

# If only a single commit, do a short version, otherwise PRs spell out the same stuff many times
if len(commit_messages) == 1:
	logger.info(f"Only one commit, using a short PR message.")
	commit_message = commit_messages[0]
	other_lines = commit_message.splitlines()[1:]
	other_lines = [line for line in other_lines if line.strip()]
	other_lines = [f"{line}" for line in other_lines]
	markdown_commit_messages.extend(other_lines)
	commits_markdown = "\n".join(markdown_commit_messages)
	full_markdown = f"{commits_markdown}"
else:
	logger.info(f"Multiple commits, using a long PR message.")
	# Let's process each commit message, massaging it into a Markdown list item
	# Each title is a Markdown list item, and any other lines are indented under it.
	for commit_message in commit_messages:
		title = commit_message.splitlines()[0].strip()
		other_lines = commit_message.splitlines()[1:]
		# remove empty lines
		other_lines = [line for line in other_lines if line.strip()]
		other_lines = [f"  {line}" for line in other_lines]
		markdown_commit_messages.append(f"- {title}")
		markdown_commit_messages.extend(other_lines)
	commits_markdown = "\n".join(markdown_commit_messages)
	full_markdown = f"#### {pr_title}\n\n{commits_markdown}"

# Show the title
logger.info(f"Title: '{pr_title}'")

# Show the markdown
logger.info(f"Markdown:\n{full_markdown}")

# Normalize the title, so it's a valid git branch name.
# Replace spaces with dashes, and remove any non-alphanumeric characters.
normalized_title = pr_title.replace(" ", "-")
normalized_title = "".join([c for c in normalized_title if c.isalnum() or c == "-"])
pr_branch_name = "pr/" + normalized_title
logger.info(f"PR branch name: '{pr_branch_name}'")
full_pr_reference_for_pr = pr_to_remote_name + ":" + pr_branch_name
logger.info(f"Full PR reference for PR: '{full_pr_reference_for_pr}'")

# Try to find an existing PR in GitHub with the same branch name.
# If found, lets update it, instead of creating a new one.
existing_pr = None
for pr in github_upstream_repo.get_pulls(state="open"):
	logger.debug(f"Considering PR: {pr.html_url} head: {pr.head.ref} user:{pr.head.user.login}")
	# make sure the owner matches
	if pr.head.user.login != gh_login_from_token:
		continue
	if pr.head.ref == pr_branch_name:
		existing_pr = pr
		logger.info(f"Found existing PR: {pr.html_url}")
		break

# Great, now show the user what we're about to do, and ask for confirmation.
logger.info(f"About to create PR branch '{pr_branch_name}' from '{upstream_reference}'")
logger.info(f"About to cherry-pick the following commits:")
for cherry_pick_sha1 in cherry_pick_sha1s:
	logger.info(f"  {cherry_pick_sha1}")
logger.info(f"About to push the PR branch to '{full_pr_reference_for_pr}'")

if existing_pr is None:
	logger.info(f"About to create a PR with the following title:\n{pr_title}")
	logger.info(f"About to create a PR with the following body:\n{full_markdown}")
else:
	logger.info(f"About to update existing PR {existing_pr.html_url} with the following title:\n{pr_title}")
	logger.info(f"About to update existing PR {existing_pr.html_url} with the following body:\n{full_markdown}")

# Ask for confirmation
confirmation = input("Continue? [y/N] ")
if confirmation != "y":
	logger.info("Aborting.")
	sys.exit(1)

# Create the PR branch, from the upstream branch. Overwrite it if it already exists.
logger.info(f"Creating PR branch '{pr_branch_name}' from '{upstream_reference}'...")
git_repo.git.checkout("-B", pr_branch_name, upstream_reference)

# Cherry-pick the commits
for cherry_pick_sha1 in cherry_pick_sha1s:
	logger.info(f"Cherry-picking commit {cherry_pick_sha1}...")
	git_repo.git.cherry_pick(cherry_pick_sha1)

# Force-Push the PR branch to the work remote
logger.info(f"Pushing PR branch '{pr_branch_name}' to '{pr_to_remote_name}'...")
git_repo.git.push("-f", pr_to_remote_name, pr_branch_name)

if existing_pr is not None:
	logger.info(f"Updating existing PR {existing_pr.html_url}...")
	# update the PR title and body
	existing_pr.edit(title=pr_title, body=full_markdown)
	logger.info(f"PR updated: {existing_pr.html_url}")
	logger.info(f"PR updated: {existing_pr.url}")
else:
	logger.info(f"Creating new PR...")
	# Use PyGitHub to create a draft pull request, from the PR branch to the upstream branch
	pr = github_upstream_repo.create_pull(
		maintainer_can_modify=False,  # dont leak my secrets
		title=pr_title,
		body=full_markdown,
		head=full_pr_reference_for_pr,
		base=upstream_branch_name,
		draft=True,
	)
	logger.info(f"PR created: {pr.html_url}")
	logger.info(f"PR created: {pr.url}")

# Checkout the work branch again, so we're clean to continue working.
logger.info(f"Checking out work branch '{work_branch_name}'...")
git_repo.git.checkout(work_branch_name)

logger.info("Done.")
