import argparse
import logging
import sys
from pathlib import Path

from git import InvalidGitRepositoryError, Repo

from prtool_tui import PrtoolApp


def parse_args():
	parser = argparse.ArgumentParser(prog="prtool", description="Dispatch GitHub PRs from Markdown-written commits.")
	parser.add_argument("revisions", nargs="*", help="commits or ranges (a..b) to preselect")
	parser.add_argument("--list", action="store_true", help="start on the PR history screen")
	parser.add_argument("--reconfigure", action="store_true", help="start on the settings screen")
	parser.add_argument("--base", help="one-off base branch override (not stored)")
	parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="don't fetch the upstream base at start")
	parser.add_argument("--no-rebase", dest="rebase", action="store_false", help="don't offer to rebase the work branch when upstream is ahead (F6 still does)")
	parser.add_argument("-v", "--verbose", action="store_true", help="debug logging, into the Log pane")
	return parser.parse_args()


def main():
	args = parse_args()
	logging.getLogger().setLevel(logging.WARNING)
	logging.getLogger("prtool").setLevel(logging.DEBUG if args.verbose else logging.INFO)
	try:
		repo = Repo(Path.cwd(), search_parent_directories=True)
	except InvalidGitRepositoryError:
		sys.exit(f"'{Path.cwd()}' is not inside a git repository.")
	PrtoolApp(args, repo).run()


if __name__ == "__main__":
	main()
