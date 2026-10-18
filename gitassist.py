#!/usr/bin/env python3
"""GitAssist - reduce the human labour of merging, rebasing and resolving
conflicts in a git repository, with automatic Windows line-ending repair.

Typical usage (run from inside your git repo):

    python gitassist.py merge origin/main        # guided merge
    python gitassist.py rebase origin/main       # guided rebase
    python gitassist.py fix-eol                  # repair CRLF/encoding on dirty files
    python gitassist.py setup-eol                # write .gitattributes + renormalize
    python gitassist.py status                   # what is unresolved right now?

Requires: Python 3.8+, git on PATH. No third-party packages.
"""

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WIN = os.name == "nt"

# Text file extensions (any language). Everything else is treated as binary
# unless it proves to be text.
TEXT_EXTS = {
    # C/C++
    ".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".inl", ".ipp",
    ".rc", ".def", ".idl", ".sln", ".vcxproj", ".vcxproj.filters", ".props", ".dsp", ".dsw",
    # build / make
    ".mak", ".mk", ".cmake", ".ac", ".am", ".in",
    # other languages
    ".py", ".pyw", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs", ".go", ".rs", ".rb",
    ".php", ".swift", ".kt", ".scala", ".m", ".mm", ".pl", ".pm", ".lua", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".asm", ".s", ".sql", ".r", ".jl", ".dart", ".groovy", ".vb",
    # data / config / docs
    ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".md", ".rst", ".txt", ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".less",
    ".tex", ".srt", ".gitignore", ".gitattributes", ".editorconfig",
}

BINARY_HINT_EXTS = {
    ".exe", ".dll", ".obj", ".o", ".a", ".lib", ".so", ".dylib", ".pdb", ".ilk", ".idb",
    ".bin", ".dat", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".icns", ".pdf",
    ".zip", ".7z", ".rar", ".gz", ".tar", ".bz2", ".xz", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".class", ".jar", ".wasm", ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".sqlite", ".db",
}

C_H_DIRS_MARKER = re.compile(r"\.(c|cc|cpp|h|hpp|cxx)$", re.IGNORECASE)


def is_c_like(path: str) -> bool:
    return bool(C_H_DIRS_MARKER.search(path))


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def git(args, cwd=None, check=True, capture=True, input_text=None):
    """Run a git command. Returns CompletedProcess. Raises on failure if check."""
    cmd = ["git"] + list(args)
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True,
        input=input_text, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed:\n{err}")
    return result


def out(args, cwd=None):
    return git(args, cwd=cwd).stdout.strip()


def out_or(args, default="", cwd=None):
    """git output or default, never raising (for probing optional state)."""
    r = git(args, cwd=cwd, check=False)
    if r.returncode != 0:
        return default
    return (r.stdout or "").strip()


class GitError(Exception):
    pass


def repo_root(cwd=None):
    r = git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if r.returncode != 0:
        raise GitError("Not inside a git repository. cd into your project first.")
    return Path(r.stdout.strip())


def in_merge_or_rebase(root: Path):
    """Return ('merge'|'rebase', dict of state info) if a conflicted op is underway."""
    g = root / ".git"
    merge_head = g / "MERGE_HEAD"
    rebase_merge = g / "rebase-merge"
    rebase_apply = g / "rebase-apply"
    if merge_head.exists():
        return "merge", {}
    if rebase_merge.exists():
        return "rebase", {}
    if rebase_apply.exists():
        return "rebase", {}
    return None, {}


# --------------------------------------------------------------------------
# line-ending / encoding inspection & repair
# --------------------------------------------------------------------------

def sniff_bytes(data: bytes):
    """Classify raw bytes. Returns (is_text, has_cr_lf, has_lone_lf, has_bom, is_utf16)."""
    if b"\x00" in data:
        return False, False, False, False, False
    bom = False
    if data.startswith(b"\xef\xbb\xbf"):
        bom = True
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        utf16 = True
        return True, False, False, bom, True
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False, False, False, bom, False
    return True, crlf > 0, (lf > 0 or cr > 0), bom, False


def looks_text_file(path: Path):
    ext = path.suffix.lower()
    if ext in BINARY_HINT_EXTS:
        return False
    if ext in TEXT_EXTS:
        return True
    # unknown extension: peek
    try:
        with open(path, "rb") as f:
            data = f.read(8192)
    except OSError:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def fix_file_eol(path: Path, target="crlf", strip_bom=True, verbose=True):
    """Normalize a single text file for Windows.

    target: 'crlf' converts all line endings to CRLF (Windows dev machine);
            'lf' converts to LF. Also re-encodes UTF-16 files to UTF-8 and
            strips UTF-8/UTF-16 BOMs.
    Returns one of: 'fixed', 'clean', 'skipped-binary', 'error'.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "error"
    if b"\x00" in data:
        return "skipped-binary"

    reasons = []
    is_utf16 = data.startswith((b"\xff\xfe", b"\xfe\xff"))
    if is_utf16:
        enc = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be"
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            return "skipped-binary"
        reasons.append("utf16->utf8")
    else:
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
            reasons.append("strip-bom")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "skipped-binary"
    if not is_utf16 and strip_bom is False and not reasons:
        pass  # no-op guard

    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    lone_cr = text.count("\r") - crlf

    if is_utf16:
        pass  # definitely re-encode
    elif lone_lf == 0 and lone_cr == 0 and not reasons:
        return "clean"

    # Normalize to LF first, then apply the target (no newline doubling)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "crlf":
        text = text.replace("\n", "\r\n")
    out_bytes = text.encode("utf-8")
    try:
        with open(path, "wb") as f:
            f.write(out_bytes)
    except OSError:
        return "error"
    if verbose:
        why = reasons[0] if reasons else "line-endings"
        print(f"    fixed {path.name}: {why} -> {target.upper()}")
    return "fixed"


# --------------------------------------------------------------------------
# interactive helpers
# --------------------------------------------------------------------------

def ask(question, choices, default=None):
    """choices: list of (key, label). Returns the chosen key (str, lowercase)."""
    keyline = "/".join(k for k, _ in choices)
    for k, label in choices:
        print(f"      [{k}] {label}")
    while True:
        hint = f" (default: {default})" if default else ""
        try:
            ans = input(f"    {question} [{keyline}]{hint}: ").strip().lower()
        except EOFError:
            ans = default or choices[0][0]
        if not ans and default:
            return default
        if ans in {k.lower() for k, _ in choices}:
            return ans
        print("      Please pick one of the listed keys.")


def ask_branch_is_latest(branch_a, branch_b):
    """Ask the developer which branch holds the newest/authoritative code."""
    print("\n  Two versions of changed code must be combined:")
    print(f"    A) {branch_a}")
    print(f"    B) {branch_b}")
    return ask("Which one should generally WIN when both changed the same lines?",
               [("a", f"keep {branch_a}"), ("b", f"keep {branch_b}"),
                ("m", "merge both (diff3, manual)"), ("i", "ask per file")],
               default="i")


# --------------------------------------------------------------------------
# conflict view / diff
# --------------------------------------------------------------------------

CONFLICT_MARKERS = re.compile(r"^(<<<<<<<|=======$|>>>>>>>|\|\|\|\|\|\|\|)", re.M)


def conflicts_list(root: Path):
    r = git(["diff", "--name-only", "--diff-filter=U"], cwd=root, check=False)
    return [line for line in r.stdout.splitlines() if line.strip()]


def show_side_by_side(root: Path, path: str):
    ours = out_or(["show", f":2:{path}"], cwd=root)
    theirs = out_or(["show", f":3:{path}"], cwd=root)
    print(f"\n  --- OURS ({'current branch'}) ---")
    print(ours or "(empty)")
    print(f"\n  --- THEIRS (incoming) ---")
    print(theirs or "(empty)")


def resolve_single_file(root: Path, path: str, resolution, auto_stage=True):
    """resolution: 'ours'|'theirs'|'both'|'diff3-manual'|'diff'|'e'|'s' (skip)."""
    if resolution == "s":
        print(f"    skipped: {path} (still unresolved)")
        return False
    if resolution == "diff":
        show_side_by_side(root, path)
        return False  # not resolved yet; caller re-asks
    if resolution in ("ours", "theirs"):
        side = "--ours" if resolution == "ours" else "--theirs"
        git(["checkout", side, "--", path], cwd=root)
    elif resolution == "both":
        # keep both versions concatenated in one file, separated by banners
        ours = out_or(["show", f":2:{path}"], cwd=root)
        theirs = out_or(["show", f":3:{path}"], cwd=root)
        text = (f"/* === OURS === */\n{ours}\n\n/* === THEIRS === */\n{theirs}\n"
                if is_c_like(path) else
                f"# === OURS ===\n{ours}\n\n# === THEIRS ===\n{theirs}\n")
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    elif resolution in ("e", "diff3-manual"):
        launch_editor(root, path)
        if CONFLICT_MARKERS.search((root / path).read_text(encoding="utf-8", errors="replace")):
            print("    conflict markers still present - not staging.")
            return False
    elif resolution == "m":  # merge tool
        git(["mergetool", "--", path], cwd=root, check=False)
    # normalize EOL before staging so the fix is committed, not carried forward
    p = root / path
    if p.exists() and looks_text_file(p):
        fix_file_eol(p, target="crlf" if IS_WIN else "lf", verbose=True)
    git(["add", "--", path], cwd=root)
    print(f"    resolved & staged: {path}")
    return True


def launch_editor(root: Path, path: str):
    editor = out_or(["var", "GIT_EDITOR"]) or os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "notepad" if IS_WIN else "vi"
    full = root / path
    subprocess.run(editor.split() + [str(full)] if editor != "notepad" else [editor, str(full)])


# --------------------------------------------------------------------------
# main flows
# --------------------------------------------------------------------------

def guided_conflict_loop(root: Path, default_winner=None, paths=None):
    """Walk every unmerged path and ask how to resolve it."""
    unmerged = paths or conflicts_list(root)
    if not unmerged:
        print("  No unmerged files.")
        return 0
    print(f"\n  {len(unmerged)} conflicted file(s).")
    resolved = []
    for i, path in enumerate(unmerged, 1):
        print(f"\n  [{i}/{len(unmerged)}] CONFLICT: {path}")
        while True:
            r = ask("How to resolve this file?",
                    [("t", "take THEIRS (incoming)"),
                     ("o", "take OURS (current)"),
                     ("b", "keep BOTH in one file"),
                     ("d", "show both versions / diff first"),
                     ("e", "edit conflict markers manually"),
                     ("m", "open git mergetool"),
                     ("s", "skip for now")],
                    default=default_winner or "d")
            if r == "d":
                show_side_by_side(root, path)
                continue
            if resolve_single_file(root, path, r):
                resolved.append(path)
            break
    still = conflicts_list(root)
    if not still:
        print("\n  All conflicts resolved and staged.")
        return 0
    print(f"\n  Still unresolved: {len(still)} -> {', '.join(still)}")
    return 1


def finish_merge_or_rebase(root: Path, kind: str, message=None):
    """Complete the operation once nothing is unmerged."""
    if conflicts_list(root):
        print("  Cannot finish: conflicts remain.")
        return 1
    if kind == "merge":
        r = git(["commit", "--no-edit"] if message is None else
                ["commit", "-m", message], cwd=root, check=False)
        if r.returncode == 0:
            print("  Merge committed.")
            return 0
        # nothing to commit?
        print((r.stderr or r.stdout or "").strip() or "  nothing to commit")
        return 0
    else:  # rebase
        r = git(["rebase", "--continue"], cwd=root, check=False, input_text="")
        print((r.stdout or "") + (r.stderr or ""))
        return r.returncode


def do_merge(root: Path, ref: str, policy: str, eol_target: str):
    git(["merge", "--no-commit", ref], cwd=root, check=False)
    if conflicts_list(root):
        print(f"\n  Merge with {ref} produced conflicts.")
        default_winner = {"theirs": "t", "ours": "o"}.get(policy)
        guided_conflict_loop(root, default_winner=default_winner)
    else:
        print("  Merged cleanly.")
    if not conflicts_list(root):
        if eol_target:
            normalize_worktree(root, eol_target, only_changed=True)
        finish_merge_or_rebase(root, "merge")
    return 0


def do_rebase(root: Path, ref: str, policy: str, eol_target: str):
    r = git(["rebase", ref], cwd=root, check=False)
    if r.returncode == 0:
        print("  Rebase completed cleanly.")
        return 0
    while True:
        kind, _ = in_merge_or_rebase(root)
        unmerged = conflicts_list(root)
        if not unmerged:
            # maybe it just needs --continue (e.g. empty commits)
            nxt = ask("No unmerged files. What next?",
                      [("c", "continue rebase"), ("s", "skip this commit"),
                       ("a", "abort rebase")], default="c")
            if nxt == "a":
                git(["rebase", "--abort"], cwd=root, check=False)
                return 1
            if nxt == "s":
                git(["rebase", "--skip"], cwd=root, check=False)
            else:
                git(["rebase", "--continue"], cwd=root, check=False, input_text="")
            r = git(["status"], cwd=root, check=False)
            if not (root / ".git" / "rebase-merge").exists() and \
               not (root / ".git" / "rebase-apply").exists():
                print("  Rebase finished.")
                return 0
            continue
        print(f"\n  Rebase stopped with {len(unmerged)} conflicted file(s).")
        default_winner = {"theirs": "t", "ours": "o"}.get(policy)
        guided_conflict_loop(root, default_winner=default_winner)
        if conflicts_list(root):
            nxt = ask("Conflicts remain. What next?",
                      [("a", "abort rebase"), ("e", "keep working"),
                       ("c", "try --continue anyway")], default="e")
            if nxt == "a":
                git(["rebase", "--abort"], cwd=root, check=False)
                print("  Rebase aborted; repository back to previous state.")
                return 1
            if nxt == "c":
                git(["rebase", "--continue"], cwd=root, check=False, input_text="")
        else:
            git(["rebase", "--continue"], cwd=root, check=False, input_text="")
        if not (root / ".git" / "rebase-merge").exists() and \
           not (root / ".git" / "rebase-apply").exists():
            print("  Rebase finished.")
            return 0


# --------------------------------------------------------------------------
# EOL commands
# --------------------------------------------------------------------------

def gitattributes_content():
    lines = ["# Auto-generated by GitAssist - normalize EOLs across OSes", "* text=auto"]
    for ext in sorted(TEXT_EXTS):
        lines.append(f"*{ext} text eol=crlf")
    for ext in sorted(BINARY_HINT_EXTS):
        lines.append(f"*{ext} binary")
    return "\n".join(lines) + "\n"


def cmd_setup_eol(args, root: Path):
    ga = root / ".gitattributes"
    content = gitattributes_content()
    if ga.exists():
        print("  .gitattributes already exists:")
        print("    " + ga.read_text(encoding="utf-8", errors="replace")[:400].replace("\n", "\n    "))
        a = ask("Overwrite it with GitAssist defaults?", [("y", "yes"), ("n", "no")], default="n")
        if a != "y":
            return 0
    ga.write_text(content, encoding="utf-8", newline="")
    print("  Wrote .gitattributes (text=auto; C/C++ and common text types -> CRLF; binaries marked).")
    git(["add", ".gitattributes"], cwd=root)
    print("  Renormalizing repository (git add --renormalize .) ...")
    r = git(["add", "--renormalize", "."], cwd=root, check=False)
    if r.returncode != 0:
        print(r.stderr.strip())
    changed = out_or(["diff", "--cached", "--name-only"], cwd=root)
    if changed:
        n = len(changed.splitlines())
        print(f"  {n} tracked file(s) normalized in the index.")
        a = ask("Commit this normalization now?", [("y", "yes"), ("n", "no")], default="y")
        if a == "y":
            git(["commit", "-m", "Normalize line endings (.gitattributes by GitAssist)"], cwd=root)
            print("  Committed.")
    else:
        print("  Nothing needed renormalizing - repository is already consistent.")
    return 0


def cmd_fix_eol(args, root: Path):
    target = args.eol or ("crlf" if IS_WIN else "lf")
    scope = args.scope
    if scope == "changed":
        names = out_or(["status", "--porcelain"], cwd=root).splitlines()
        paths = [line[3:].strip().strip('"') for line in names if line[3:].strip()]
    else:
        paths = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                 and ".git" not in p.relative_to(root).parts]
    fixed = clean = skipped = err = 0
    for name in paths:
        p = root / name
        if not p.exists() or not looks_text_file(p):
            skipped += 1
            continue
        res = fix_file_eol(p, target=target, strip_bom=True, verbose=args.verbose)
        if res == "fixed":
            fixed += 1
            if not args.verbose:
                print(f"    fixed: {name}")
        elif res == "clean":
            clean += 1
        elif res == "skipped-binary":
            skipped += 1
        else:
            err += 1
    print(f"\n  done. fixed={fixed} clean={clean} skipped(binary/unknown)={skipped} errors={err}")
    if fixed and not args.dry_run:
        a = ask("Stage the fixed files?", [("y", "yes"), ("n", "no")], default="y")
        if a == "y":
            git(["add", "-u"], cwd=root)
            print("  Staged. Review with: git diff --cached --stat")
    return 0


def normalize_worktree(root: Path, target, only_changed=False):
    names = out_or(["status", "--porcelain"], cwd=root).splitlines()
    files = [line[3:].strip().strip('"') for line in names if line[3:].strip()]
    n = 0
    for name in files:
        p = root / name
        if p.exists() and looks_text_file(p):
            if fix_file_eol(p, target=target, verbose=True) == "fixed":
                n += 1
                git(["add", "--", name], cwd=root, check=False)
    print(f"  Normalized {n} file(s) to {target.upper()} before committing.")


def cmd_status(root: Path):
    kind, _ = in_merge_or_rebase(root)
    branch = out_or(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root) or "(detached)"
    print(f"  branch: {branch}")
    if kind == "merge":
        print("  state : MERGE in progress")
    elif kind == "rebase":
        print("  state : REBASE in progress")
    else:
        print("  state : clean operation")
    r = git(["status", "--porcelain"], cwd=root)
    print(r.stdout.rstrip() or "  (nothing pending)")
    unmerged = conflicts_list(root)
    if unmerged:
        print(f"  unmerged: {len(unmerged)}")
        for u in unmerged:
            print(f"    U {u}")
    return 0


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="gitassist", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-C", "--repo", default=None, help="path to the repo (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("merge", help="guided merge of a branch into the current one")
    p.add_argument("ref")
    p.add_argument("--policy", choices=["interactive", "ours", "theirs"], default="interactive",
                   help="default winner when both sides changed the same lines")
    p.add_argument("--eol", choices=["crlf", "lf", "none"], default=None,
                   help="normalize files to this line ending before committing (default: crlf on Windows, lf elsewhere)")

    p = sub.add_parser("rebase", help="guided rebase of the current branch onto a ref")
    p.add_argument("ref")
    p.add_argument("--policy", choices=["interactive", "ours", "theirs"], default="interactive")
    p.add_argument("--eol", choices=["crlf", "lf", "none"], default=None)

    p = sub.add_parser("resolve", help="resolve conflicts from an ongoing merge/rebase")
    p.add_argument("--policy", choices=["interactive", "ours", "theirs"], default="interactive")

    p = sub.add_parser("fix-eol", help="repair line endings/encoding of text files")
    p.add_argument("--scope", choices=["changed", "all"], default="changed")
    p.add_argument("--eol", choices=["crlf", "lf"], default=None)
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("setup-eol", help="write .gitattributes and renormalize the repo")
    sub.add_parser("status", help="show merge/rebase state and conflicts")
    sub.add_parser("version", help="print version")

    args = ap.parse_args(argv)
    try:
        root = Path(args.repo).resolve() if args.repo else repo_root()
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print("GitAssist")
    if args.cmd == "status":
        return cmd_status(root)
    if args.cmd == "version":
        print("gitassist 1.0.0")
        return 0
    if args.cmd == "setup-eol":
        return cmd_setup_eol(args, root)
    if args.cmd == "fix-eol":
        return cmd_fix_eol(args, root)
    if args.cmd == "resolve":
        rc = guided_conflict_loop(root)
        if rc == 0:
            kind, _ = in_merge_or_rebase(root)
            if kind:
                return finish_merge_or_rebase(root, kind)
        return rc
    if args.cmd == "merge":
        return do_merge(root, args.ref, args.policy, args.eol)
    if args.cmd == "rebase":
        return do_rebase(root, args.ref, args.policy, args.eol)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
