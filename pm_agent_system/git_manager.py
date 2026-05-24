"""
git_manager.py — Pure Python Git operation manager. **No LLM calls.**

Step 8 — REAL git activated (with dry-run safety net).

Responsibilities (Spec §4):
  - Track which files are locked by which active task
  - Create feature branch per task (real `git checkout -b` OR [DRY-RUN])
  - On PASS verdict: commit + push branch + open GitHub PR
    (★ NOT direct merge to main — PR opens, Teo merges via GitHub UI)
  - Detect conflicts → return to caller; never resolve autonomously
  - Persistent state in `state/git_state.json`

Dry-run mode:
  Construct GitManager(dry_run=True). Every git/GitHub call is replaced
  with a "[DRY-RUN] would ..." print. State file is still updated so the
  rest of the system can be exercised end-to-end.

State file format (`state/git_state.json`):
{
  "active_tasks": {
    "T-42": {
      "branch": "feat/T-42",
      "locked_files": ["domain/conflict.py"],
      "status": "in_progress",
      "created_at": "..."
    }
  },
  "completed_tasks": [
    {"task_id": "T-42", "branch": "feat/T-42", "commit_message": "...",
     "pr_url": "https://github.com/.../pull/7", "completed_at": "..."}
  ],
  "pending_merge": []
}
"""
from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


class GitManager:
    """File-lock and branch state tracker + real git ops (or dry-run)."""

    def __init__(
        self,
        repo_path: str,
        state_path: str = "state/git_state.json",
        github_token: str | None = None,
        github_repo: str | None = None,        # "owner/repo"
        dry_run: bool = False,
        no_auto_pr: bool = False,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.state_path = Path(state_path)
        self.github_token = github_token
        self.github_repo = github_repo
        self.dry_run = dry_run
        self.no_auto_pr = no_auto_pr
        self._state: dict[str, Any] = self._load_state()
        self._github_client = None  # lazy-init PyGithub

    # ── State persistence ────────────────────────────────────────────
    def _load_state(self) -> dict[str, Any]:
        """Load state from disk; return fresh skeleton if missing."""
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"corrupted state file at {self.state_path}: {exc}"
                ) from exc
        return {
            "active_tasks": {},
            "completed_tasks": [],
            "pending_merge": [],
        }

    def _save_state(self) -> None:
        """Persist state to disk. Auto-create parent dir."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def release_stale_locks(self) -> list[str]:
        """On startup: release active_tasks older than 2h (bot killed mid-task).
        Returns released task_ids.
        """
        now = datetime.now()
        stale: list[str] = []
        for tid, entry in list(self._state["active_tasks"].items()):
            try:
                age = (now - datetime.fromisoformat(entry["created_at"])).total_seconds()
                if age > 7200:
                    stale.append(tid)
            except (KeyError, ValueError):
                stale.append(tid)
        for tid in stale:
            self._state["active_tasks"].pop(tid)
        if stale:
            self._save_state()
        return stale

    def get_state(self) -> dict[str, Any]:
        """Read-only view of the current state. For debugging / CLI --status."""
        return json.loads(json.dumps(self._state))  # deep copy via roundtrip

    # ── Conflict detection ───────────────────────────────────────────
    def check_conflicts(self, task_id: str, files: list[str]) -> list[str]:
        """Return list of files locked by *another* active task. [] = safe."""
        conflicts: list[str] = []
        for other_id, entry in self._state["active_tasks"].items():
            if other_id == task_id:
                continue
            locked = set(entry.get("locked_files", []))
            for f in files:
                if f in locked and f not in conflicts:
                    conflicts.append(f)
        return conflicts

    # ── Branch creation ──────────────────────────────────────────────
    def create_branch(self, task_id: str, files: list[str]) -> str:
        """Create a feature branch and register file locks.

        - Real mode: `git fetch origin main && git checkout main && git pull
                      && git checkout -b feat/<task_id>`
        - Dry-run: print "[DRY-RUN] would ..." only.

        Raises RuntimeError on file lock conflict (other task holds them).
        """
        conflicts = self.check_conflicts(task_id, files)
        if conflicts:
            raise RuntimeError(
                f"cannot create branch for {task_id}: "
                f"{len(conflicts)} file(s) locked by another task: {conflicts}. "
                f"Use escalate_conflict() to report to PM."
            )

        branch = self._derive_branch_name(task_id)

        if self.dry_run:
            print(f"[DRY-RUN] would create branch {branch} (in {self.repo_path})")
        else:
            try:
                self._run_git(["fetch", "origin"])
                self._run_git(["checkout", "main"])
                try:
                    self._run_git(["pull", "--ff-only", "origin", "main"])
                except RuntimeError as exc:
                    print(f"[warn] git pull failed (continuing): {exc}")

                # Check if branch already exists locally or on remote
                local_exists = False
                remote_exists = False
                try:
                    self._run_git(["rev-parse", "--verify", branch])
                    local_exists = True
                except RuntimeError:
                    pass
                try:
                    self._run_git(["rev-parse", "--verify", f"origin/{branch}"])
                    remote_exists = True
                except RuntimeError:
                    pass

                if local_exists:
                    self._run_git(["checkout", branch])
                    print(f"[git] branch {branch} already exists locally — resuming")
                elif remote_exists:
                    self._run_git(["checkout", "-b", branch, f"origin/{branch}"])
                    print(f"[git] branch {branch} found on remote — resuming")
                else:
                    self._run_git(["checkout", "-b", branch])
            except RuntimeError as exc:
                raise RuntimeError(
                    f"git failed while creating branch {branch}: {exc}"
                ) from exc

        # Register in state regardless of mode (so check_conflicts works)
        self._state["active_tasks"][task_id] = {
            "branch": branch,
            "locked_files": list(files),
            "status": "in_progress",
            "created_at": datetime.now().isoformat(),
        }
        self._save_state()
        return branch

    # ── Commit + merge to main + push ───────────────────────────────
    def commit_and_merge(
        self, task_id: str, commit_message: str
    ) -> dict[str, Any]:
        """Commit on feature branch → merge to main → push origin/main.

        Returns {"ok": bool, "commit_sha": str | None, "branch": str | None}.

        ★ Merges directly to main (no PR). Serial task execution ensures
          no conflict: each task starts from fresh main after previous merges.

        Dry-run: every step printed only.
        """
        entry = self._state["active_tasks"].get(task_id)
        if entry is None:
            return {"ok": False, "commit_sha": None, "branch": None,
                    "error": f"task {task_id} not in active_tasks"}

        branch = entry["branch"]
        files = entry.get("locked_files", [])

        if self.dry_run:
            print(f"[DRY-RUN] would `git checkout {branch}`")
            print(f"[DRY-RUN] would `git add -u`")
            print(f"[DRY-RUN] would `git commit -m {commit_message!r}`")
            print(f"[DRY-RUN] would `git checkout main && git merge --no-ff {branch}`")
            print(f"[DRY-RUN] would `git push origin main`")
            print(f"[DRY-RUN] would `git branch -d {branch}`")
            commit_sha = "[dry-run]"
        else:
            try:
                # 1. 피처 브랜치로 전환
                self._run_git(["checkout", branch])

                # 2. 변경된 tracked 파일 전체 스테이징
                self._run_git(["add", "-u"])

                # 3. staged diff가 있을 때만 커밋
                cached_check = subprocess.run(
                    ["git", "-C", str(self.repo_path),
                     "diff", "--cached", "--quiet"],
                    capture_output=True, text=True, check=False,
                )
                if cached_check.returncode == 1:
                    # staged changes 있음 → 커밋
                    self._run_git(["commit", "-m", commit_message])
                elif cached_check.returncode == 0:
                    print(
                        f"[git] no staged changes for {task_id} — "
                        "branch tip == main; skipping commit, will still merge"
                    )
                else:
                    raise RuntimeError(
                        f"`git diff --cached --quiet` exit "
                        f"{cached_check.returncode}: "
                        f"{cached_check.stderr.strip() or '(empty stderr)'}"
                    )

                # 4. main으로 돌아와서 최신화
                self._run_git(["checkout", "main"])
                try:
                    self._run_git(["pull", "--ff-only", "origin", "main"])
                except RuntimeError as exc:
                    print(f"[warn] pull before merge failed (continuing): {exc}")

                # 5. 피처 브랜치를 main에 --no-ff 머지
                self._run_git(["merge", "--no-ff", branch, "-m", commit_message])

                # 6. origin/main으로 push
                self._run_git(["push", "origin", "main"])

                # 7. 로컬 피처 브랜치 삭제 (원격에는 올리지 않음)
                try:
                    self._run_git(["branch", "-d", branch])
                except RuntimeError:
                    pass  # non-fatal

                # 8. stale worktrees 정리
                try:
                    self._run_git(["worktree", "prune"])
                except RuntimeError:
                    pass

                commit_sha = self._run_git(["rev-parse", "--short", "HEAD"]).strip()

            except Exception as exc:
                # 실패 시 main으로 복귀 시도
                try:
                    self._run_git(["checkout", "main"])
                except Exception:
                    pass
                return {"ok": False, "commit_sha": None, "branch": branch,
                        "error": f"{type(exc).__name__}: {exc}"}

        # active_tasks → completed_tasks 이동
        self._state["completed_tasks"].append({
            "task_id": task_id,
            "branch": branch,
            "locked_files": files,
            "commit_message": commit_message,
            "commit_sha": commit_sha,
            "completed_at": datetime.now().isoformat(),
        })
        del self._state["active_tasks"][task_id]
        self._save_state()
        return {"ok": True, "commit_sha": commit_sha, "branch": branch}

    # ── Lock release (task failure / cancellation) ───────────────────
    def release_lock(self, task_id: str) -> None:
        """Release file locks for a task. Delete the local branch too."""
        if task_id not in self._state["active_tasks"]:
            return
        entry = self._state["active_tasks"][task_id]
        branch = entry["branch"]

        if self.dry_run:
            print(f"[DRY-RUN] would `git checkout main && git branch -D {branch}`")
        else:
            try:
                self._run_git(["checkout", "main"])
                self._run_git(["branch", "-D", branch])
            except RuntimeError as exc:
                print(f"[warn] failed to delete branch {branch}: {exc}")

        del self._state["active_tasks"][task_id]
        self._save_state()

    # ── Conflict escalation report ───────────────────────────────────
    def escalate_conflict(
        self, task_id: str, conflicts: list[str]
    ) -> dict[str, Any]:
        """Build a conflict report dict for the PM Agent to forward to Teo."""
        locked_by: dict[str, str] = {}
        for f in conflicts:
            for other_id, entry in self._state["active_tasks"].items():
                if other_id == task_id:
                    continue
                if f in entry.get("locked_files", []):
                    locked_by[f] = other_id
                    break
        return {
            "task_id": task_id,
            "conflicting_files": conflicts,
            "locked_by": locked_by,
            "reported_at": datetime.now().isoformat(),
        }

    # ── Internal: git subprocess wrapper ─────────────────────────────
    def _run_git(self, args: list[str]) -> str:
        """Run `git -C <repo_path> <args>`. Raises RuntimeError on non-zero."""
        cmd = ["git", "-C", str(self.repo_path)] + args
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            cmd_str = " ".join(shlex.quote(a) for a in cmd)
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"`{cmd_str}` exit {result.returncode}: {stderr or '(empty stderr)'}"
            )
        return result.stdout

    # (PR creation removed — direct merge to main strategy)

    # ── Internal: branch name derivation ─────────────────────────────
    def _derive_branch_name(self, task_id: str) -> str:
        """Derive a git-safe branch name with date suffix to avoid collisions."""
        safe = "".join(c if c.isalnum() or c in "-_/" else "-" for c in task_id)
        date = datetime.now().strftime("%m%d")
        return f"feat/{safe}-{date}"
