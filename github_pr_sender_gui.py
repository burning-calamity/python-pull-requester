#!/usr/bin/env python3
"""
GitHub Pull Request Sender GUI

A defensive Tkinter utility that takes either a local folder or a Git patch
(.apply/.patch/.diff) and opens a pull request against a GitHub repository.

Requirements:
    - Python 3.10+
    - Git
    - GitHub CLI (`gh`)

Recommended one-time setup:
    gh auth login

The program performs preflight checks before changing anything and can repair
several common local problems automatically, including Git identity, GitHub CLI
credential-helper setup, invalid/colliding branch names, stale forks, and
transient network failures.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Sequence


APP_TITLE = "GitHub Pull Request Sender"
DEFAULT_COMMIT = "Add files via GitHub PR Sender"
DEFAULT_PR_TITLE = "Add/update files"
COMMAND_TIMEOUT = 180
NETWORK_RETRIES = 3


# ---------------------------------------------------------------------------
# Process / validation helpers
# ---------------------------------------------------------------------------

class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args_list = list(args)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        detail = self.stderr.strip() or self.stdout.strip() or f"exit code {returncode}"
        super().__init__(f"{format_command(args)}\n{detail}")


def format_command(args: Sequence[str]) -> str:
    def quote(s: str) -> str:
        if not s or re.search(r"\s|[\"']", s):
            return '"' + s.replace('"', '\\"') + '"'
        return s
    return " ".join(quote(str(a)) for a in args)


def run_process(
    args: Sequence[str],
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: int = COMMAND_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and without hidden interactive prompts."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    if extra_env:
        env.update(extra_env)

    try:
        p = subprocess.run(
            [str(x) for x in args],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {format_command(args)}") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {args[0]}") from exc

    if check and p.returncode != 0:
        raise CommandError(args, p.returncode, p.stdout, p.stderr)
    return p


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def looks_transient(text: str) -> bool:
    s = text.lower()
    needles = (
        "timed out", "timeout", "connection reset", "connection refused",
        "could not resolve host", "temporary failure", "tls", "http 502",
        "http 503", "http 504", "bad gateway", "service unavailable",
        "remote end hung up", "failed to connect", "unexpected eof",
    )
    return any(n in s for n in needles)


def run_with_retry(
    args: Sequence[str],
    cwd: Path | str | None = None,
    attempts: int = NETWORK_RETRIES,
    logger=None,
    timeout: int = COMMAND_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return run_process(args, cwd=cwd, timeout=timeout, extra_env=extra_env)
        except Exception as exc:
            last = exc
            if attempt >= attempts or not looks_transient(str(exc)):
                raise
            delay = 1.5 * attempt
            if logger:
                logger(f"Transient error; retrying ({attempt}/{attempts}) in {delay:.1f}s...")
            time.sleep(delay)
    assert last is not None
    raise last


def normalize_repo(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Repository is required.")

    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        return value.removesuffix(".git")

    # HTTPS, SSH-like git@github.com:owner/repo.git, or github.com/owner/repo.
    m = re.search(
        r"(?:https?://)?(?:www\.)?github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$",
        value,
        flags=re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)}/{m.group(2)}"

    raise ValueError("Repository must be OWNER/REPO or a GitHub repository URL.")


def sanitize_branch_name(value: str) -> str:
    """Best-effort conversion to a branch name accepted by Git."""
    value = value.strip().replace("\\", "/")
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9._/-]+", "-", value)
    value = value.replace("@{", "-")
    value = re.sub(r"/+", "/", value)
    value = re.sub(r"\.{2,}", ".", value)

    parts: list[str] = []
    for component in value.split("/"):
        component = component.strip()
        component = component.lstrip(".")
        component = component.rstrip(".")
        if component.lower().endswith(".lock"):
            component = component[:-5] + "-lock"
        if not component or component == "@":
            continue
        parts.append(component)

    value = "/".join(parts).strip("/.-")
    value = value.lstrip("-")
    if not value or value == "@":
        value = f"folder-pr-{time.strftime('%Y%m%d-%H%M%S')}"
    return value


def validate_destination(value: str) -> str:
    value = value.strip().replace("\\", "/").strip("/")
    if not value:
        return ""
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise ValueError("Destination subfolder must be a safe relative path and may not contain '..'.")
    if any(part.lower() == ".git" for part in p.parts):
        raise ValueError("Destination may not be inside .git.")
    return p.as_posix()


def folder_has_payload(source: Path) -> bool:
    try:
        return any(item.name != ".git" for item in source.iterdir())
    except OSError:
        return False


def count_payload(source: Path) -> tuple[int, int]:
    files = 0
    dirs = 0
    for root, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        dirs += len(dirnames)
        files += len(filenames)
    return files, dirs


def copy_folder_contents(source: Path, destination: Path, target_subdir: str = "") -> None:
    """Merge-copy source contents while never copying source .git metadata."""
    dest_root = destination / target_subdir if target_subdir else destination
    dest_root.mkdir(parents=True, exist_ok=True)

    for item in source.iterdir():
        if item.name == ".git":
            continue
        dst = dest_root / item.name
        if item.is_symlink():
            # Preserve symlinks on platforms that allow it; give a useful error otherwise.
            target = os.readlink(item)
            if dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            os.symlink(target, dst, target_is_directory=item.is_dir())
        elif item.is_dir():
            shutil.copytree(
                item,
                dst,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)




PATCH_EXTENSIONS = {".apply", ".patch", ".diff"}


def source_kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    if path.is_file() and path.suffix.lower() in PATCH_EXTENSIONS:
        return "apply"
    return ""


def _decode_printf_single_quoted_payload(raw: str) -> str:
    """Decode the POSIX-shell quote escape used by generated .apply wrappers."""
    return raw.replace("'\\''", "'")


def extract_patch_text(path: Path) -> str:
    """
    Extract a raw Git/unified patch from either a raw patch file or a generated
    .apply wrapper. The wrapper is parsed as data; no shell code is executed.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Could not read patch file: {exc}") from exc

    if not raw_bytes:
        raise ValueError("The selected patch file is empty.")

    try:
        payload_file = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "The selected patch file is not UTF-8 text. Binary apply wrappers are not supported."
        ) from exc

    normalized = payload_file.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip()

    if stripped.startswith("diff --git ") or stripped.startswith("--- "):
        return stripped

    start_match = re.search(r"printf\s+'%s'\s+'", normalized)
    if not start_match:
        raise ValueError(
            "Unsupported .apply format. Expected a raw Git patch or a wrapper containing "
            "printf '%s' '...' | git apply ..."
        )

    end_match = re.search(
        r"'\s*\|\s*git\s+apply(?:\s+[^)]*)?\)\s*$",
        normalized,
        flags=re.DOTALL,
    )
    if not end_match or end_match.start() <= start_match.end():
        raise ValueError("Could not locate the end of the embedded patch payload.")

    encoded = normalized[start_match.end():end_match.start()]
    patch = _decode_printf_single_quoted_payload(encoded).lstrip()
    if not patch.startswith(("diff --git ", "--- ")):
        raise ValueError("The .apply wrapper was found, but its payload is not a Git patch.")
    return patch


def patch_paths(patch_text: str) -> list[str]:
    """Extract changed paths from Git-style diff headers."""
    import shlex

    paths: list[str] = []
    seen: set[str] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line[len("diff --git "):], posix=True)
        except ValueError:
            parts = line[len("diff --git "):].split()
        if len(parts) < 2:
            continue
        candidate = parts[1]
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        candidate = candidate.replace("\\", "/")
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def validate_patch_paths(paths: Sequence[str]) -> None:
    for item in paths:
        p = PurePosixPath(item)
        if p.is_absolute() or any(part in ("", "..") for part in p.parts):
            raise ValueError(f"Unsafe path in patch: {item}")
        if p.parts and p.parts[0].lower() == ".git":
            raise ValueError(f"Patch may not modify .git metadata: {item}")


def inspect_patch_file(path: Path) -> tuple[str, list[str]]:
    patch = extract_patch_text(path)
    paths = patch_paths(patch)
    if paths:
        validate_patch_paths(paths)
    return patch, paths


def _rollback_patch_attempt(workdir: Path) -> None:
    """Return the temporary clone to the branch HEAD after a failed patch attempt."""
    run_process(["git", "reset", "--hard", "HEAD"], cwd=workdir, check=False, timeout=60)
    run_process(["git", "clean", "-fd"], cwd=workdir, check=False, timeout=60)


def _unmerged_paths(workdir: Path) -> list[str]:
    """Return paths currently left in a Git merge-conflict state."""
    result = run_process(
        ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=workdir,
        check=False,
        timeout=60,
    )
    if not result.stdout:
        return []
    return [p for p in result.stdout.split("\0") if p]


def _has_stage(workdir: Path, path: str, stage: int) -> bool:
    """Check whether an unmerged path has the requested index stage."""
    result = run_process(
        ["git", "ls-files", "-u", "--", path],
        cwd=workdir,
        check=False,
        timeout=60,
    )
    for line in result.stdout.splitlines():
        # Format: <mode> <object> <stage>\t<path>
        head = line.split("\t", 1)[0].split()
        if len(head) >= 3:
            try:
                if int(head[2]) == stage:
                    return True
            except ValueError:
                pass
    return False


def _resolve_three_way_conflicts_with_patch(
    workdir: Path,
    conflicts: Sequence[str],
    logger=None,
) -> None:
    """
    Resolve conflicts created by `git apply --3way` by choosing the patch side
    ("theirs") only for the paths that actually conflict.

    Cleanly merged files are left untouched. For a deletion conflict where the
    patch side has no stage-3 blob, the patch's deletion is accepted.
    """
    if logger:
        logger(
            f"Auto-fix: {len(conflicts)} file(s) need conflict resolution. "
            "Using the patch version only for those conflicting files."
        )

    for path in conflicts:
        if _has_stage(workdir, path, 3):
            result = run_process(
                ["git", "checkout", "--theirs", "--", path],
                cwd=workdir,
                check=False,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not select the patch version for conflicted file: {path}\n"
                    f"{(result.stderr or result.stdout).strip()}"
                )
            run_process(["git", "add", "--", path], cwd=workdir, timeout=60)
            if logger:
                logger(f"  resolved with patch version: {path}")
        else:
            # No stage 3 means the patch side deleted the path.
            result = run_process(
                ["git", "rm", "-f", "--ignore-unmatch", "--", path],
                cwd=workdir,
                check=False,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not accept the patch deletion for conflicted file: {path}\n"
                    f"{(result.stderr or result.stdout).strip()}"
                )
            if logger:
                logger(f"  resolved with patch deletion: {path}")

    remaining = _unmerged_paths(workdir)
    if remaining:
        raise RuntimeError(
            "Some patch conflicts could not be resolved automatically:\n"
            + "\n".join(f"  {p}" for p in remaining[:30])
        )


def _compact_git_apply_error(detail: str, limit: int = 20) -> str:
    """
    Keep only the useful end of git-apply diagnostics so a popup is not filled
    with hundreds of successful 'Applied patch ... cleanly' lines.
    """
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    if not lines:
        return "git apply failed"

    important = [
        line for line in lines
        if (
            "error:" in line.lower()
            or "conflict" in line.lower()
            or "failed" in line.lower()
            or "does not apply" in line.lower()
            or "does not exist" in line.lower()
            or "already exists" in line.lower()
        )
    ]
    chosen = important[-limit:] if important else lines[-limit:]
    return "\n".join(chosen)


def apply_patch_safely(
    source: Path,
    workdir: Path,
    scratch_dir: Path,
    auto_fix: bool,
    logger=None,
) -> list[str]:
    """
    Apply .apply/.patch/.diff input without executing its shell wrapper.

    Strategy:
      1. Parse and path-check the patch.
      2. Run a real `git apply --3way` (not `--check`).
      3. If Git creates merge conflicts and Auto-fix is enabled, keep every
         clean three-way merge and choose the patch side only for conflicted
         paths.
      4. If no usable 3-way state was produced, roll back and try ordinary
         apply modes.
    """
    patch, paths = inspect_patch_file(source)
    patch_file = scratch_dir / "incoming-change.patch"
    patch_file.write_text(patch, encoding="utf-8", newline="\n")

    if logger:
        if paths:
            logger(f"Patch contains changes for {len(paths)} path(s).")
            for item in paths[:20]:
                logger(f"  patch: {item}")
            if len(paths) > 20:
                logger(f"  ... and {len(paths) - 20} more path(s)")
        else:
            logger("Patch uses unified-diff format; Git will determine the affected paths.")

    failures: list[str] = []

    # ------------------------------------------------------------------
    # First choice: a real 3-way apply.
    #
    # Important: do NOT use `git apply --check --3way` here. A check returns
    # failure for the whole patch if even one path conflicts, even though Git
    # can successfully merge most other paths and can expose a resolvable
    # conflict state for the remaining ones.
    # ------------------------------------------------------------------
    if logger:
        logger("Trying 3-way patch application...")

    three_way = run_process(
        ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_file)],
        cwd=workdir,
        check=False,
        timeout=300,
    )

    if three_way.returncode == 0:
        if logger:
            logger("OK: patch applied cleanly with a 3-way merge.")
        return paths

    three_way_detail = (three_way.stderr or three_way.stdout).strip()
    conflicts = _unmerged_paths(workdir)

    if conflicts:
        if logger:
            logger(
                f"3-way apply produced {len(conflicts)} conflict(s), while other "
                "files were applied/merged successfully."
            )
            for item in conflicts[:30]:
                logger(f"  conflict: {item}")
            if len(conflicts) > 30:
                logger(f"  ... and {len(conflicts) - 30} more conflict(s)")

        if auto_fix:
            try:
                _resolve_three_way_conflicts_with_patch(workdir, conflicts, logger=logger)

                # Make sure the repository is no longer in an unmerged state.
                remaining = _unmerged_paths(workdir)
                if remaining:
                    raise RuntimeError(
                        "Unresolved conflicts remain:\n"
                        + "\n".join(f"  {p}" for p in remaining[:30])
                    )

                if logger:
                    logger(
                        "OK: 3-way conflicts were automatically resolved. "
                        "Clean merges were preserved; only conflicting paths used "
                        "the patch version."
                    )
                return paths
            except Exception as exc:
                failures.append(f"3-way conflict auto-fix: {exc}")
                if logger:
                    logger(f"3-way conflict auto-fix failed: {exc}")
                _rollback_patch_attempt(workdir)
        else:
            failures.append(
                "3-way apply created conflicts:\n"
                + "\n".join(f"  {p}" for p in conflicts[:30])
            )
            _rollback_patch_attempt(workdir)
    else:
        failures.append(
            "3-way apply: " + _compact_git_apply_error(three_way_detail)
        )
        _rollback_patch_attempt(workdir)

    # ------------------------------------------------------------------
    # Fallbacks for patches that cannot form a 3-way state.
    # Ordinary git apply is atomic in the normal failure case; we still reset
    # between attempts defensively.
    # ------------------------------------------------------------------
    attempts: list[tuple[str, list[str]]] = [
        ("normal apply", ["--whitespace=nowarn"]),
    ]
    if auto_fix:
        attempts.append(
            (
                "whitespace-tolerant apply",
                ["--ignore-space-change", "--ignore-whitespace", "--whitespace=nowarn"],
            )
        )

    for description, options in attempts:
        if logger:
            logger(f"Trying {description}...")

        applied = run_process(
            ["git", "apply", *options, str(patch_file)],
            cwd=workdir,
            check=False,
            timeout=300,
        )
        if applied.returncode == 0:
            if logger:
                logger(f"OK: patch applied using {description}.")
            return paths

        detail = (applied.stderr or applied.stdout).strip()
        failures.append(f"{description}: {_compact_git_apply_error(detail)}")
        _rollback_patch_attempt(workdir)

    # Keep the popup concise; the full detailed progress remains in the GUI log.
    summary = "\n\n".join(failures[-3:])
    raise RuntimeError(
        "The .apply/patch file could not be applied to the selected base branch."
        + (f"\n\n{summary}" if summary else "")
        + "\n\nThe complete Git output is available in the Progress / output panel."
    )


def create_gh_askpass(root: Path) -> tuple[Path, dict[str, str]]:
    """
    Create a temporary Git ask-pass helper that obtains the token directly from
    the already-authenticated GitHub CLI. The token is never written to disk or
    included in a command line/log message.
    """
    gh_path = shutil.which("gh")
    if not gh_path:
        raise RuntimeError("GitHub CLI ('gh') was not found in PATH.")

    root.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        helper = root / "gh-askpass.cmd"
        # For a username prompt, use GitHub's documented token username.
        # For the password prompt, ask gh for the current account token.
        body = (
            "@echo off\r\n"
            "setlocal\r\n"
            "echo %~1 | findstr /I /C:\"Username\" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            "  echo x-access-token\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            f'"{gh_path}" auth token\r\n'
        )
        helper.write_text(body, encoding="utf-8", newline="")
    else:
        helper = root / "gh-askpass.sh"
        safe_gh = "'" + gh_path.replace("'", "'\"'\"'") + "'"
        body = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*|*username*) printf "%s\\n" "x-access-token" ;;\n'
            f"  *) exec {safe_gh} auth token ;;\n"
            "esac\n"
        )
        helper.write_text(body, encoding="utf-8")
        helper.chmod(0o700)

    env = {
        "GIT_ASKPASS": str(helper),
        "GIT_ASKPASS_REQUIRE": "force",
        "GCM_INTERACTIVE": "Never",
    }
    return helper, env


def git_network_args(*args: str) -> list[str]:
    """
    Disable any stale configured credential helper for this one Git invocation.
    This forces Git to use our temporary GIT_ASKPASS bridge instead.
    """
    return ["git", "-c", "credential.helper=", *args]


def robust_rmtree(path: Path, logger=None, attempts: int = 6) -> bool:
    """Remove a Git working tree on Windows even when files became read-only."""
    if not path.exists():
        return True

    def onerror(func, target, exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except Exception:
            pass

    last_error = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=onerror)
            return True
        except Exception as exc:
            last_error = exc
            time.sleep(0.35 * (attempt + 1))

    # A rename often succeeds even if antivirus/indexing briefly holds a child.
    try:
        renamed = path.with_name(path.name + f"_cleanup_{int(time.time())}")
        path.rename(renamed)
        if logger:
            logger(f"Cleanup deferred: renamed locked workspace to {renamed}")
        return False
    except Exception:
        if logger and last_error is not None:
            logger(f"Cleanup warning: could not fully remove {path}: {last_error}")
        return False


@dataclass(frozen=True)
class PRConfig:
    source: Path
    source_kind: str
    username: str
    target_repo: str
    base: str
    branch: str
    destination: str
    commit_message: str
    pr_title: str
    pr_body: str
    draft: bool
    keep_temp: bool
    auto_fix: bool


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PRApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("930x750")
        self.minsize(790, 620)

        self.folder_var = tk.StringVar()
        self.username_var = tk.StringVar()
        self.repo_var = tk.StringVar()
        self.base_var = tk.StringVar()
        self.branch_var = tk.StringVar(value=self.make_branch_name())
        self.dest_var = tk.StringVar()
        self.commit_var = tk.StringVar(value=DEFAULT_COMMIT)
        self.title_var = tk.StringVar(value=DEFAULT_PR_TITLE)
        self.draft_var = tk.BooleanVar(value=False)
        self.keep_temp_var = tk.BooleanVar(value=False)
        self.auto_fix_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")

        self._busy = False
        self._build_ui()
        self.after(250, self.detect_account)

    @staticmethod
    def make_branch_name() -> str:
        return f"folder-pr-{time.strftime('%Y%m%d-%H%M%S')}"

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(form, text="Source folder / patch:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.folder_var).grid(row=row, column=1, sticky="ew", padx=8)
        source_buttons = ttk.Frame(form)
        source_buttons.grid(row=row, column=2, sticky="w")
        ttk.Button(source_buttons, text="Folder...", command=self.choose_folder).pack(side="left")
        ttk.Button(source_buttons, text=".apply...", command=self.choose_apply_file).pack(side="left", padx=(5, 0))
        row += 1

        ttk.Label(form, text="GitHub username:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.username_var).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="Detect", command=self.detect_account).grid(row=row, column=2)
        row += 1

        ttk.Label(form, text="Target repository:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.repo_var).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="OWNER/REPO").grid(row=row, column=2, sticky="w")
        row += 1

        ttk.Label(form, text="Base branch:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.base_var).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="blank = auto").grid(row=row, column=2, sticky="w")
        row += 1

        ttk.Label(form, text="New branch:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.branch_var).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="New name", command=lambda: self.branch_var.set(self.make_branch_name())).grid(row=row, column=2)
        row += 1

        ttk.Label(form, text="Destination subfolder:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.dest_var).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="blank = repo root").grid(row=row, column=2, sticky="w")
        row += 1

        ttk.Label(form, text="Commit message:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.commit_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=8)
        row += 1

        ttk.Label(form, text="PR title:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.title_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=8)
        row += 1

        ttk.Label(form, text="PR description:").grid(row=row, column=0, sticky="nw", pady=5)
        self.body_text = tk.Text(form, height=6, wrap="word")
        self.body_text.grid(row=row, column=1, columnspan=2, sticky="nsew", padx=8)
        self.body_text.insert(
            "1.0",
            "Files added with GitHub Pull Request Sender.\n\nPlease review the changes before merging.",
        )
        row += 1

        opts = ttk.Frame(form)
        opts.grid(row=row, column=1, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(opts, text="Create as draft PR", variable=self.draft_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opts, text="Keep temporary clone", variable=self.keep_temp_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opts, text="Auto-fix errors / patch conflicts", variable=self.auto_fix_var).pack(side="left")

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(12, 8))

        self.send_btn = ttk.Button(action, text="Check + Create Pull Request", command=self.start)
        self.send_btn.pack(side="left")
        self.check_btn = ttk.Button(action, text="Run checks", command=self.start_diagnostics)
        self.check_btn.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(action, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(action, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="Checks / repairs / output", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

    # -------------------- UI-safe helpers --------------------

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)

    def choose_apply_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose .apply / patch file",
            filetypes=[
                ("Apply and patch files", "*.apply *.patch *.diff"),
                ("Apply files", "*.apply"),
                ("Patch files", "*.patch *.diff"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.folder_var.set(filename)
            self.dest_var.set("")
            name = Path(filename).name
            if self.commit_var.get().strip() == DEFAULT_COMMIT:
                self.commit_var.set(f"Apply changes from {name}")
            if self.title_var.get().strip() == DEFAULT_PR_TITLE:
                self.title_var.set(f"Apply changes from {name}")
            self.append_log(f"Selected patch source: {filename}")

    def append_log(self, text: str) -> None:
        def do_append() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, do_append)

    def set_status(self, text: str) -> None:
        self.after(0, self.status_var.set, text)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

        def do_set() -> None:
            state = "disabled" if busy else "normal"
            self.send_btn.configure(state=state)
            self.check_btn.configure(state=state)
            if busy:
                self.progress.start(10)
            else:
                self.progress.stop()
        self.after(0, do_set)

    def snapshot_config(self, require_repo: bool = True) -> PRConfig:
        """Read ALL Tk values on the main thread; workers never touch Tk widgets."""
        source_text = self.folder_var.get().strip()
        source = Path(source_text).expanduser().resolve() if source_text else Path()
        username = self.username_var.get().strip()
        repo_text = self.repo_var.get().strip()
        target_repo = normalize_repo(repo_text) if repo_text else ""
        base = self.base_var.get().strip()
        raw_branch = self.branch_var.get().strip()
        branch = sanitize_branch_name(raw_branch)
        destination = validate_destination(self.dest_var.get())
        commit = self.commit_var.get().strip()
        title = self.title_var.get().strip()
        body = self.body_text.get("1.0", "end-1c").strip()

        kind = source_kind(source) if source_text else ""
        if not source_text or not kind:
            raise ValueError("Choose a valid source folder or a .apply/.patch/.diff file.")
        if not os.access(source, os.R_OK):
            raise ValueError("The selected source is not readable.")
        if kind == "folder" and not folder_has_payload(source):
            raise ValueError("The selected source folder contains no files to send.")
        if kind == "apply" and source.stat().st_size == 0:
            raise ValueError("The selected patch file is empty.")
        if kind == "apply" and destination:
            destination = ""
            self.dest_var.set("")
            self.append_log("Auto-fix: destination subfolder is ignored for patch files.")
        if require_repo and not target_repo:
            raise ValueError("Enter a target GitHub repository.")
        if not commit:
            raise ValueError("Commit message cannot be empty.")
        if not title:
            raise ValueError("Pull request title cannot be empty.")

        if raw_branch != branch:
            self.branch_var.set(branch)
            self.append_log(f"Auto-fix: sanitized branch name to '{branch}'.")

        return PRConfig(
            source=source,
            source_kind=kind,
            username=username,
            target_repo=target_repo,
            base=base,
            branch=branch,
            destination=destination,
            commit_message=commit,
            pr_title=title,
            pr_body=body,
            draft=bool(self.draft_var.get()),
            keep_temp=bool(self.keep_temp_var.get()),
            auto_fix=bool(self.auto_fix_var.get()),
        )

    # -------------------- Account detection --------------------

    def detect_account(self) -> None:
        def worker() -> None:
            try:
                if command_exists("gh"):
                    p = run_process(["gh", "api", "user", "--jq", ".login"], check=False, timeout=30)
                    name = p.stdout.strip()
                    if p.returncode == 0 and name:
                        self.after(0, self.username_var.set, name)
                        self.append_log(f"Detected GitHub account: {name}")
                        return
                if command_exists("git"):
                    for key in ("github.user", "user.name"):
                        p = run_process(["git", "config", "--global", key], check=False, timeout=15)
                        name = p.stdout.strip()
                        if p.returncode == 0 and name:
                            self.after(0, self.username_var.set, name)
                            self.append_log(f"Fallback account value from git config ({key}): {name}")
                            return
                self.append_log("Could not automatically detect a GitHub username.")
            except Exception as exc:
                self.append_log(f"Username detection failed: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    # -------------------- Diagnostics / repairs --------------------

    def start_diagnostics(self) -> None:
        if self._busy:
            return
        try:
            cfg = self.snapshot_config(require_repo=False)
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self.set_busy(True)
        threading.Thread(target=self._diagnostics_worker, args=(cfg,), daemon=True).start()

    def _diagnostics_worker(self, cfg: PRConfig) -> None:
        try:
            self.append_log("=" * 72)
            self.append_log("Running checks...")
            self.run_local_checks(cfg)
            if cfg.target_repo:
                self.run_github_checks(cfg, repair=cfg.auto_fix)
            self.set_status("Checks passed")
            self.append_log("All requested checks passed.")
            self.after(0, lambda: messagebox.showinfo("Checks complete", "Checks completed successfully."))
        except Exception as exc:
            self.set_status("Check failed")
            self.append_log(f"CHECK FAILED: {exc}")
            self.after(0, lambda e=str(exc): messagebox.showerror("Check failed", e))
        finally:
            self.set_busy(False)

    def run_local_checks(self, cfg: PRConfig) -> None:
        self.set_status("Checking local tools...")
        if not command_exists("git"):
            raise RuntimeError("Git was not found in PATH. Install Git and restart the application.")
        if not command_exists("gh"):
            raise RuntimeError("GitHub CLI ('gh') was not found in PATH. Install GitHub CLI and restart the application.")

        git_v = run_process(["git", "--version"], timeout=20).stdout.strip()
        gh_v = run_process(["gh", "--version"], timeout=20).stdout.splitlines()[0].strip()
        self.append_log(f"OK: {git_v}")
        self.append_log(f"OK: {gh_v}")

        if cfg.source_kind == "folder":
            files, dirs = count_payload(cfg.source)
            self.append_log(f"OK: source folder is readable ({files} files, {dirs} folders).")
        else:
            patch_text, paths = inspect_patch_file(cfg.source)
            size_kib = cfg.source.stat().st_size / 1024.0
            if paths:
                self.append_log(
                    f"OK: patch file is readable ({size_kib:.1f} KiB, {len(paths)} changed path(s))."
                )
            else:
                self.append_log(
                    f"OK: patch file is readable ({size_kib:.1f} KiB, unified-diff format)."
                )
            if not patch_text.strip():
                raise RuntimeError("The selected patch contains no patch data.")

        # Verify we can create/write/remove a temp directory before cloning.
        probe_root = Path(tempfile.mkdtemp(prefix="github_pr_sender_probe_"))
        try:
            probe = probe_root / "write-test.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)
        self.append_log("OK: temporary workspace is writable.")

        # Validate branch using the installed Git, not only our sanitizer.
        branch_test = run_process(["git", "check-ref-format", "--branch", cfg.branch], check=False, timeout=20)
        if branch_test.returncode != 0:
            raise RuntimeError(f"Git rejects branch name '{cfg.branch}'. Choose another branch name.")
        self.append_log(f"OK: branch name '{cfg.branch}' is valid.")

    def ensure_auth(self, auto_fix: bool) -> str:
        self.set_status("Checking GitHub authentication...")
        auth = run_process(["gh", "auth", "status", "--hostname", "github.com"], check=False, timeout=30)
        if auth.returncode != 0:
            raise RuntimeError(
                "GitHub CLI is not authenticated. Run this once in PowerShell/Terminal:\n\n"
                "gh auth login\n\nThen retry. Interactive login is intentionally not launched inside the GUI."
            )

        user = run_with_retry(["gh", "api", "user", "--jq", ".login"], logger=self.append_log, timeout=45).stdout.strip()
        if not user:
            raise RuntimeError("GitHub authentication succeeded, but the account name could not be determined.")
        self.append_log(f"OK: authenticated to GitHub as {user}.")

        # Verify that gh can actually provide an HTTPS token for Git. Never log it.
        token_probe = run_process(["gh", "auth", "token"], check=False, timeout=30)
        if token_probe.returncode != 0 or not token_probe.stdout.strip():
            raise RuntimeError(
                "GitHub CLI is logged in, but it cannot provide a token for Git HTTPS authentication.\n"
                "Run: gh auth login --hostname github.com"
            )
        self.append_log("OK: GitHub CLI can provide Git HTTPS credentials.")

        if auto_fix:
            setup = run_process(["gh", "auth", "setup-git"], check=False, timeout=30)
            if setup.returncode == 0:
                self.append_log("Auto-fix: Git credential helper is configured through GitHub CLI.")
            else:
                self.append_log("Warning: could not configure Git credential helper automatically.")
        return user

    def read_repo(self, target_repo: str) -> tuple[str, str]:
        p = run_with_retry(
            [
                "gh", "repo", "view", target_repo,
                "--json", "nameWithOwner,defaultBranchRef",
                "--jq", '.nameWithOwner + "\\t" + (.defaultBranchRef.name // "")',
            ],
            logger=self.append_log,
            timeout=60,
        )
        fields = p.stdout.strip().split("\t")
        if len(fields) != 2 or not fields[0]:
            raise RuntimeError("Could not read the target repository metadata.")
        if not fields[1]:
            raise RuntimeError("The target repository appears to be empty and has no default branch to base a pull request on.")
        return fields[0], fields[1]

    def verify_base_branch(self, target_repo: str, base: str) -> None:
        ref = run_process(
            ["gh", "api", f"repos/{target_repo}/git/ref/heads/{base}", "--jq", ".ref"],
            check=False,
            timeout=45,
        )
        if ref.returncode != 0:
            raise RuntimeError(f"Base branch '{base}' does not exist in {target_repo}.")
        self.append_log(f"OK: base branch '{base}' exists.")

    def run_github_checks(self, cfg: PRConfig, repair: bool) -> tuple[str, str, str]:
        user = self.ensure_auth(repair)
        target_repo, default_branch = self.read_repo(cfg.target_repo)
        base = cfg.base or default_branch
        self.append_log(f"OK: target repository is {target_repo}; default branch is {default_branch}.")
        self.verify_base_branch(target_repo, base)
        return user, target_repo, base

    # -------------------- Main workflow --------------------

    def start(self) -> None:
        if self._busy:
            return
        try:
            cfg = self.snapshot_config(require_repo=True)
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self.set_busy(True)
        threading.Thread(target=self._workflow, args=(cfg,), daemon=True).start()

    def ensure_fork(self, target_repo: str, username: str, base: str, auto_fix: bool) -> str:
        repo_name = target_repo.split("/", 1)[1]
        fork_repo = f"{username}/{repo_name}"

        self.set_status("Checking fork...")
        info = run_process(
            ["gh", "api", f"repos/{fork_repo}", "--jq", '[.fork, (.parent.full_name // "")] | @tsv'],
            check=False,
            timeout=45,
        )
        if info.returncode == 0:
            parts = info.stdout.strip().split("\t")
            is_fork = parts and parts[0].lower() == "true"
            parent = parts[1] if len(parts) > 1 else ""
            if not is_fork or parent.lower() != target_repo.lower():
                raise RuntimeError(
                    f"{fork_repo} already exists but is not a fork of {target_repo}.\n"
                    "GitHub cannot create the expected fork with the same repository name."
                )
            self.append_log(f"OK: existing fork {fork_repo} points to {target_repo}.")
        else:
            self.set_status("Creating fork...")
            self.append_log(f"Creating fork {fork_repo}...")
            run_with_retry(
                ["gh", "repo", "fork", target_repo, "--clone=false", "--remote=false"],
                logger=self.append_log,
                timeout=120,
            )
            for attempt in range(1, 21):
                time.sleep(min(1.0 + attempt * 0.2, 3.0))
                check = run_process(["gh", "api", f"repos/{fork_repo}", "--jq", ".full_name"], check=False, timeout=30)
                if check.returncode == 0:
                    self.append_log(f"OK: fork created as {fork_repo}.")
                    break
            else:
                raise RuntimeError("GitHub accepted the fork request, but the fork did not become available in time.")

        if auto_fix:
            # GitHub's fork sync endpoint safely brings the fork's base branch forward.
            sync = run_process(
                ["gh", "api", "--method", "POST", f"repos/{fork_repo}/merge-upstream", "-f", f"branch={base}"],
                check=False,
                timeout=60,
            )
            if sync.returncode == 0:
                self.append_log("Auto-fix: fork base branch synchronized with upstream.")
            else:
                self.append_log("Note: automatic fork sync was not available; the workflow will use upstream directly.")
        return fork_repo

    def unique_remote_branch(
        self,
        remote_url: str,
        requested: str,
        auto_fix: bool,
        auth_env: dict[str, str],
    ) -> str:
        branch = requested
        for n in range(0, 100):
            candidate = branch if n == 0 else f"{branch}-{n + 1}"
            p = run_process(
                git_network_args("ls-remote", "--heads", remote_url, candidate),
                check=False,
                timeout=45,
                extra_env=auth_env,
            )
            if p.returncode != 0 and looks_transient(p.stderr + p.stdout):
                p = run_process(
                    git_network_args("ls-remote", "--heads", remote_url, candidate),
                    check=False,
                    timeout=45,
                    extra_env=auth_env,
                )
            if p.returncode != 0:
                # Permission/auth errors should not be misread as "branch available".
                raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "Could not inspect remote branches.")
            if not p.stdout.strip():
                if candidate != requested:
                    self.append_log(f"Auto-fix: branch already existed; using '{candidate}' instead.")
                return candidate
            if not auto_fix:
                raise RuntimeError(f"Branch '{requested}' already exists on the push repository.")
        raise RuntimeError("Could not find a free branch name after 100 attempts.")

    def _workflow(self, cfg: PRConfig) -> None:
        temp_root: Path | None = None
        auth_root: Path | None = None
        auth_env: dict[str, str] | None = None
        try:
            self.append_log("=" * 72)
            self.append_log(
                f"Source {'folder' if cfg.source_kind == 'folder' else 'patch file'}: {cfg.source}"
            )
            self.append_log(f"Target: {cfg.target_repo}")

            # Full preflight: local tools, auth, repository, base branch.
            self.run_local_checks(cfg)
            username, target_repo, base = self.run_github_checks(cfg, repair=cfg.auto_fix)

            # Build a temporary credential bridge for Git HTTPS operations.
            # This avoids relying on whatever credential helper happens to be
            # configured globally on the machine.
            auth_root = Path(tempfile.mkdtemp(prefix="github_pr_sender_auth_"))
            _, auth_env = create_gh_askpass(auth_root)
            self.append_log("OK: secure Git credential bridge is ready.")

            if cfg.username and cfg.username.lower() != username.lower():
                self.append_log(f"Auto-fix: authenticated account is {username}; replacing entered username '{cfg.username}'.")
                self.after(0, self.username_var.set, username)

            # Permission detection through GitHub API.
            self.set_status("Checking repository permissions...")
            perm = run_with_retry(
                ["gh", "api", f"repos/{target_repo}", "--jq", ".permissions.push"],
                logger=self.append_log,
                timeout=45,
            )
            can_push_target = perm.stdout.strip().lower() == "true"

            if can_push_target:
                push_repo = target_repo
                self.append_log("OK: authenticated account has push access to the target repository.")
            else:
                self.append_log("No direct push access; a fork will be used.")
                push_repo = self.ensure_fork(target_repo, username, base, cfg.auto_fix)

            target_url = f"https://github.com/{target_repo}.git"
            push_url = f"https://github.com/{push_repo}.git"
            assert auth_env is not None
            branch = self.unique_remote_branch(push_url, cfg.branch, cfg.auto_fix, auth_env)
            if branch != cfg.branch:
                self.after(0, self.branch_var.set, branch)

            # Clone the TARGET, even in fork mode. This prevents stale-fork/default-branch
            # problems. We later repoint origin to the fork when necessary.
            self.set_status("Cloning clean base...")
            temp_root = Path(tempfile.mkdtemp(prefix="github_pr_sender_"))
            workdir = temp_root / "repo"
            self.append_log(f"Temporary workspace: {temp_root}")
            run_with_retry(
                git_network_args("clone", "--branch", base, "--single-branch", "--", target_url, str(workdir)),
                logger=self.append_log,
                timeout=180,
                extra_env=auth_env,
            )

            if push_repo.lower() != target_repo.lower():
                run_process(["git", "remote", "rename", "origin", "upstream"], cwd=workdir)
                run_process(["git", "remote", "add", "origin", push_url], cwd=workdir)
            else:
                # Ensure origin is canonical even if git normalized it differently.
                run_process(["git", "remote", "set-url", "origin", push_url], cwd=workdir)

            self.set_status("Creating branch...")
            run_process(["git", "checkout", "-b", branch], cwd=workdir)

            if cfg.source_kind == "folder":
                self.set_status("Copying files...")
                self.append_log(
                    "Copying folder contents to "
                    + (f"'{cfg.destination}'..." if cfg.destination else "repository root...")
                )
                copy_folder_contents(cfg.source, workdir, cfg.destination)
            else:
                self.set_status("Checking and applying patch...")
                self.append_log(
                    "Safely parsing .apply/patch file. The shell wrapper will NOT be executed."
                )
                apply_patch_safely(
                    cfg.source,
                    workdir,
                    temp_root,
                    cfg.auto_fix,
                    logger=self.append_log,
                )

            self.set_status("Checking changes...")
            status = run_process(["git", "status", "--porcelain"], cwd=workdir)
            if not status.stdout.strip():
                raise RuntimeError("The selected source produced no changes compared with the target repository.")
            self.append_log("Changes detected:")
            for line in status.stdout.splitlines():
                self.append_log("  " + line)

            self.set_status("Preparing commit...")
            run_process(["git", "add", "-A"], cwd=workdir)

            # Automatic local-only identity repair. This does not overwrite global config.
            git_name = run_process(["git", "config", "user.name"], cwd=workdir, check=False).stdout.strip()
            git_email = run_process(["git", "config", "user.email"], cwd=workdir, check=False).stdout.strip()
            if not git_name:
                run_process(["git", "config", "user.name", username], cwd=workdir)
                self.append_log(f"Auto-fix: set repository-local Git user.name to {username}.")
            if not git_email:
                noreply = f"{username}@users.noreply.github.com"
                run_process(["git", "config", "user.email", noreply], cwd=workdir)
                self.append_log(f"Auto-fix: set repository-local Git user.email to {noreply}.")

            run_process(["git", "commit", "-m", cfg.commit_message], cwd=workdir)

            self.set_status("Pushing branch...")
            try:
                run_with_retry(
                    git_network_args("push", "-u", "origin", branch),
                    cwd=workdir,
                    logger=self.append_log,
                    timeout=180,
                    extra_env=auth_env,
                )
            except Exception as first_push_error:
                if not cfg.auto_fix:
                    raise
                # Most common repair: Git credential helper was not wired to gh yet.
                self.append_log(f"Push failed: {first_push_error}")
                self.append_log("Auto-fix: refreshing GitHub CLI Git credentials and retrying once...")
                run_process(["gh", "auth", "setup-git"], check=False, timeout=30)
                # Re-create the bridge in case gh authentication changed.
                if auth_root is not None:
                    _, auth_env = create_gh_askpass(auth_root)
                run_with_retry(
                    git_network_args("push", "-u", "origin", branch),
                    cwd=workdir,
                    logger=self.append_log,
                    timeout=180,
                    extra_env=auth_env,
                )

            self.set_status("Checking for existing PR...")
            head = branch if can_push_target else f"{username}:{branch}"
            existing = run_process(
                [
                    "gh", "pr", "list", "--repo", target_repo,
                    "--state", "open", "--head", head,
                    "--json", "url", "--jq", ".[0].url // \"\"",
                ],
                check=False,
                cwd=workdir,
                timeout=45,
            )
            if existing.returncode == 0 and existing.stdout.strip():
                pr_url = existing.stdout.strip()
                self.append_log(f"Auto-fix: an open PR already exists for this branch: {pr_url}")
            else:
                self.set_status("Opening pull request...")
                pr_cmd = [
                    "gh", "pr", "create",
                    "--repo", target_repo,
                    "--base", base,
                    "--head", head,
                    "--title", cfg.pr_title,
                    "--body", cfg.pr_body,
                ]
                if cfg.draft:
                    pr_cmd.append("--draft")
                pr = run_with_retry(pr_cmd, cwd=workdir, logger=self.append_log, timeout=120)
                pr_url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""

            self.append_log("Pull request workflow completed successfully.")
            if pr_url:
                self.append_log(pr_url)
            self.set_status("Done")
            self.after(
                0,
                lambda url=pr_url: messagebox.showinfo(
                    "Pull request ready",
                    "Pull request workflow completed successfully." + (f"\n\n{url}" if url else ""),
                ),
            )

        except Exception as exc:
            self.append_log(f"ERROR: {exc}")
            self.append_log(self.human_error_hint(str(exc)))
            self.set_status("Failed")
            self.after(
                0,
                lambda e=str(exc): messagebox.showerror(
                    "Pull request failed",
                    e if len(e) <= 5000 else e[:4800] + "\n\n[Message shortened; see Progress / output for full details.]",
                ),
            )
        finally:
            if temp_root is not None:
                if cfg.keep_temp:
                    self.append_log(f"Temporary clone kept at: {temp_root}")
                else:
                    robust_rmtree(temp_root, logger=self.append_log)

            # Authentication helper never contains the token, but remove it anyway.
            if auth_root is not None:
                robust_rmtree(auth_root, logger=self.append_log)

            self.set_busy(False)

    @staticmethod
    def human_error_hint(message: str) -> str:
        s = message.lower()
        if "could not be applied to the selected base branch" in s or "patch failed" in s:
            return (
                "Hint: verify that the target repository and base branch are the ones "
                "the .apply file was created from. The app already tries 3-way and "
                "safe whitespace-tolerant fallbacks."
            )
        if "could not read username" in s or "terminal prompts disabled" in s:
            return (
                "Hint: Git did not receive GitHub credentials. This version automatically uses "
                "the authenticated GitHub CLI token through a temporary ask-pass bridge."
            )
        if "not authenticated" in s or "gh auth login" in s:
            return "Hint: run 'gh auth login' in PowerShell/Terminal, finish browser authentication, then retry."
        if "permission denied" in s or "403" in s:
            return "Hint: check repository access/fork permissions and the scopes of the account authenticated in gh."
        if "could not resolve host" in s or "failed to connect" in s:
            return "Hint: GitHub could not be reached. Check DNS, proxy/VPN, firewall, and internet connectivity."
        if "protected branch" in s:
            return "Hint: the program pushes a separate branch; verify repository rules permit your account/fork to create branches."
        if "filename too long" in s:
            return "Hint: on Windows, enable long paths and/or shorten deeply nested source paths."
        if "symlink" in s and os.name == "nt":
            return "Hint: Windows may require Developer Mode or elevated privileges to create symbolic links."
        if "fork" in s:
            return "Hint: some organizations/private repositories disable forking. Direct write access may be required."
        return "Review the log above; no destructive repair was attempted."


if __name__ == "__main__":
    PRApp().mainloop()
