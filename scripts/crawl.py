import json
import os
import subprocess
import sys
import time
from collections import deque

from roblox_client import (
    RobloxAuthError,
    RobloxClient,
    RobloxError,
    RobloxRateLimited,
    RobloxUserError,
)

USERS_PATH = "users.txt"
STATE_PATH = "crawl-state.json"
STATE_VERSION = 1
COMMIT_EVERY_PAGES = 200
COMMIT_EVERY_SECONDS = 300
DEFAULT_BUDGET_SECONDS = 16200


def log(message):
    print(message, flush=True)


def read_user_ids(path):
    order = []
    seen = set()
    if not os.path.exists(path):
        return order
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = int(line)
            except ValueError:
                continue
            if value > 0 and value not in seen:
                seen.add(value)
                order.append(value)
    return order


def write_user_ids(path, order):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for user_id in order:
            handle.write("%d\n" % user_id)
    os.replace(tmp, path)


def read_state(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise RobloxError("state file is not an object")
    if state.get("version") != STATE_VERSION:
        raise RobloxError("unsupported state version %r" % (state.get("version"),))
    return state


def write_state(path, state):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def run_git(args, cwd=None, check=True):
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if check and result.returncode != 0:
        raise RobloxError("git %s failed: %s" % (args[0], result.stderr.strip()))
    return result


def git_committer(users_path, state_path, sleep=time.sleep):
    repo_dir = os.path.dirname(os.path.abspath(users_path)) or "."

    def commit(reason, user_count, queue_size):
        run_git(["add", "--", users_path, state_path], cwd=repo_dir)
        if run_git(["diff", "--cached", "--quiet"], cwd=repo_dir, check=False).returncode == 0:
            return
        message = "crawl: %d users known, %d queued (%s)" % (user_count, queue_size, reason)
        run_git(["commit", "-m", message], cwd=repo_dir)
        for attempt in range(5):
            push = run_git(["push"], cwd=repo_dir, check=False)
            if push.returncode == 0:
                return
            sleep(min(30, 2 ** attempt))
            run_git(["pull", "--rebase", "--autostash"], cwd=repo_dir, check=False)
        raise RobloxError("git push failed after retries")

    return commit


class Crawler:
    def __init__(
        self,
        client,
        seed,
        users_path=USERS_PATH,
        state_path=STATE_PATH,
        budget_seconds=DEFAULT_BUDGET_SECONDS,
        clock=time.monotonic,
        committer=None,
        logger=log,
    ):
        self.client = client
        self.seed = seed
        self.users_path = users_path
        self.state_path = state_path
        self.budget_seconds = budget_seconds
        self.clock = clock
        self.committer = committer if committer is not None else git_committer(
            users_path, state_path
        )
        self.logger = logger
        self.users = set()
        self.user_order = []
        self.visited = set()
        self.queue = deque()
        self.queued = set()
        self.state = None
        self.started_at = None
        self.last_commit_at = None
        self.pages_since_commit = 0
        self.pages_fetched = 0

    def _budget_exhausted(self):
        return (self.clock() - self.started_at) >= self.budget_seconds

    def load(self):
        self.user_order = read_user_ids(self.users_path)
        self.users = set(self.user_order)
        state = read_state(self.state_path)
        if state is None:
            root = self.client.resolve_user_id(self.seed)
            state = {
                "version": STATE_VERSION,
                "root_user_id": root,
                "queue": [],
                "visited": [],
                "current": None,
                "finished": False,
            }
        if state.get("root_user_id") and int(state["root_user_id"]) not in self.users:
            self.users.add(int(state["root_user_id"]))
            self.user_order.append(int(state["root_user_id"]))
        root_id = int(state.get("root_user_id") or 0)
        self.state = state
        self.visited = set(int(value) for value in state.get("visited") or [])
        self.queue = deque(int(value) for value in state.get("queue") or [])
        self.queued = set(self.queue)
        if root_id > 0:
            self._enqueue(root_id)

    def _enqueue(self, user_id):
        if user_id <= 0:
            return
        if user_id in self.visited or user_id in self.queued:
            return
        self.queue.append(user_id)
        self.queued.add(user_id)

    def _record_friends(self, ids):
        added = 0
        for user_id in ids:
            if user_id not in self.users:
                self.users.add(user_id)
                self.user_order.append(user_id)
                added += 1
            self._enqueue(user_id)
        return added

    def _should_commit(self):
        if self.pages_since_commit >= COMMIT_EVERY_PAGES:
            return True
        return (self.clock() - self.last_commit_at) >= COMMIT_EVERY_SECONDS

    def _commit(self, reason):
        self._save()
        self.committer(reason, len(self.users), len(self.queue))
        self.pages_since_commit = 0
        self.last_commit_at = self.clock()
        self.logger(
            "commit (%s): %d users, %d visited, %d queued"
            % (reason, len(self.users), len(self.visited), len(self.queue))
        )

    def _save(self):
        self.state["queue"] = list(self.queue)
        self.state["visited"] = sorted(self.visited)
        self.state["finished"] = bool(self.state.get("finished"))
        write_user_ids(self.users_path, self.user_order)
        write_state(self.state_path, self.state)

    def _scrape_current(self):
        current = self.state["current"]
        user_id = int(current["user_id"])
        cursor = current.get("cursor")
        seen = set()
        if cursor:
            seen.add(cursor)
        restarted = False
        while True:
            if self._budget_exhausted():
                return
            try:
                ids, next_cursor = self.client.friends_page(user_id, cursor)
            except (RobloxAuthError, RobloxRateLimited):
                raise
            except RobloxUserError:
                if cursor and not restarted:
                    self.logger("user %d: saved cursor rejected, restarting list" % user_id)
                    cursor = None
                    seen.clear()
                    restarted = True
                    current["cursor"] = None
                    self.state["current"] = current
                    continue
                self.logger("skip user %d: rejected by api" % user_id)
                self.state["current"] = None
                return
            except RobloxError as error:
                self.logger("skip user %d: %s" % (user_id, self.client.redact(error)))
                self.state["current"] = None
                return
            self.pages_fetched += 1
            added = self._record_friends(ids)
            self.pages_since_commit += 1
            self.logger(
                "user %d page %d: %d friends, %d new"
                % (user_id, self.pages_fetched, len(ids), added)
            )
            if not next_cursor or next_cursor in seen:
                self.state["current"] = None
                return
            seen.add(next_cursor)
            cursor = next_cursor
            current["cursor"] = cursor
            self.state["current"] = current
            if self._should_commit():
                self._commit("mid-list")

    def run(self):
        self.started_at = self.clock()
        self.last_commit_at = self.started_at
        self.load()
        if self.state.get("finished"):
            self.logger("crawl already finished; nothing to do")
            return 0
        status = 0
        while True:
            if self._budget_exhausted():
                self.logger("run budget exhausted; stopping before next user")
                break
            if self.state["current"] is None:
                if not self.queue:
                    self.state["finished"] = True
                    self._save()
                    self.logger("queue empty; crawl complete with %d users" % len(self.users))
                    break
                user_id = self.queue.popleft()
                self.queued.discard(user_id)
                self.visited.add(user_id)
                self.state["current"] = {"user_id": user_id, "cursor": None}
            try:
                self._scrape_current()
            except RobloxAuthError as error:
                self.logger("authentication failed: %s" % self.client.redact(error))
                status = 2
                break
            except RobloxRateLimited as error:
                self.logger(
                    "rate limited for %ss; stopping to resume later"
                    % int(error.retry_after)
                )
                break
            if self.state["current"] is None and self._should_commit():
                self._commit("user-complete")
        self._commit("exit")
        self.logger(
            "run finished: %d users known, %d visited, %d queued, %d pages, finished=%s"
            % (
                len(self.users),
                len(self.visited),
                len(self.queue),
                self.pages_fetched,
                self.state.get("finished"),
            )
        )
        return status


def positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def main():
    cookie = os.environ.get("ROBLOSECURITY", "").strip()
    seed = os.environ.get("SEED_USER", "").strip()
    budget = positive_int(os.environ.get("RUN_BUDGET_SECONDS"), DEFAULT_BUDGET_SECONDS)
    if not cookie:
        log("ROBLOSECURITY is not set")
        return 2
    if not seed:
        log("SEED_USER is not set")
        return 2
    client = RobloxClient(cookie)
    crawler = Crawler(client, seed, budget_seconds=budget)
    try:
        return crawler.run()
    except RobloxAuthError as error:
        log("authentication failed: %s" % client.redact(error))
        return 2
    except RobloxError as error:
        log("crawl failed: %s" % client.redact(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
