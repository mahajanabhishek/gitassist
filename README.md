# GitAssist

A small Python 3 command-line utility that reduces the human labour of
**merging, rebasing and resolving conflicts** in git — designed for a
**Windows development machine** with C/C++ code, but it works for any
text-based language.

## What it does

1. **Guided merge** — `gitassist merge <ref>` runs the merge and, when
conflicts appear, walks you through every conflicted file one at a time:

   * show both versions side by side
   * take THEIRS (incoming) / OURS (current)
   * keep BOTH in one file
   * open a manual editor on the conflict markers
   * open `git mergetool`
2. **Guided rebase** — `gitassist rebase <ref>` handles each stopped commit:
resolve, continue, skip, or abort (repository is restored safely).
3. **Ask-the-developer intelligence** — before resolving it can ask which
branch holds the "latest" code and apply that as the default winner
(`--policy ours|theirs|interactive`).
4. **Windows line-ending repair** — any file it touches (or that you point
it at) is converted to CRLF, UTF-8, BOM-free before it is staged, so
Linux-produced LF/UTF-16 files are fixed automatically before entering
the repo. Binary files (`.exe .obj .lib .png` …) are detected and never
touched.
5. **Repo-wide normalization** — `gitassist setup-eol` writes a
`.gitattributes` (`\* text=auto`, `\*.c/\*.cpp/\*.h/... eol=crlf`,
binary extensions marked `binary`) and runs
`git add --renormalize .`, then offers to commit it.

## Requirements

* Python 3.8+ (no third-party packages)
* git on PATH

## Install / use (Windows)

```bat
:: put gitassist.py anywhere and run it from inside your repo
python C:\\tools\\gitassist\\gitassist.py status
python C:\\tools\\gitassist\\gitassist.py setup-eol      :: once per repo
python C:\\tools\\gitassist\\gitassist.py merge origin/main
python C:\\tools\\gitassist\\gitassist.py rebase origin/main
python C:\\tools\\gitassist\\gitassist.py resolve        :: resume an in-flight conflict
python C:\\tools\\gitassist\\gitassist.py fix-eol        :: repair EOLs of changed files
python C:\\tools\\gitassist\\gitassist.py fix-eol --scope all --eol crlf --verbose
```

Tip: drop the folder on your PATH or create `gitassist.bat` next to it:

```bat
@echo off
python "%\~dp0gitassist.py" %\*
```

## Typical workflow (Linux code → Windows repo)

1. Developer edits/commits on Linux (LF files land in the repo).
2. On the Windows machine:

```
   gitassist setup-eol          # one-time repo policy
   gitassist merge origin/feature-x
   ```

3. For each conflict GitAssist asks which version to keep, fixes line
endings/encoding of whatever is staged, and commits the merge.

## Safety

* Nothing is committed until every conflict is resolved and you confirm.
* `rebase --abort` is one keystroke away; abort restores the previous state.
* Binary files are skipped automatically.

