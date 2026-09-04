# Lessons

Corrections and the patterns that prevent repeating them. Newest first.

---

## 2026-09-04 - Do not let convenience silently make a structural decision

**Correction from the maintainer:** "Why did you put it in .tools and not in
<volume>\Projects\Utilities-Tools\Metadata-Scrubber"

The launchers were put in the shared `.tools\bin\` for one reason: that
directory was already on PATH, so a bare `metascrub` worked immediately. That
convenience was allowed to decide the structure, and the trade-off against
project self-containment was never weighed or surfaced.

It was the wrong call. Launchers are project artifacts. Putting them in a shared
bucket meant the project directory alone was not runnable, and the shared
directory would accumulate a launcher per project.

**Pattern:** when one option is easier to wire up, that is not a reason, it is a
pull. Say out loud which structural property is being traded away, and offer the
choice rather than absorbing it. The fix here was a split the user should have
been offered up front: real launchers in the project, thin forwarders in the
PATH directory, the shared third-party binary left shared.

---

## 2026-09-04 - A green suite is not evidence when the failure lives off the tested path

138 tests passed while `metascrub scrub` reported every file CLEAN, verified
clean, exit 0, and changed nothing. The defect only opened when exiftool was
absent, and the suite ran on a machine that had it.

The smoke test that found it was one command on a real file, run before writing
any code, because the question asked was "how do I run this".

**Pattern:** run the thing once, as a user would, on a machine that is not
already set up. Ask what happens when a dependency is missing rather than
present. A test suite proves the paths it exercises and nothing about the ones
that only open on a different machine.

---

## 2026-09-04 - Two states that must never be confused must never share a type

The root cause of the above: `ExifSession.read()` caught every exception and
returned `{}`, so "could not read this file" and "this file carries nothing"
were the same value. The caller guessed, and guessed wrong.

Its own docstring claimed the caller distinguished them via the verification
verdict. It did not. The CLEAN branch returned before verification ran.

**Pattern:** fix the type, not the call site. Patching the one function observed
to be wrong leaves the next reader free to reintroduce it. And a comment
asserting an invariant is not the invariant; if it is load-bearing, a test has
to hold it.

---

## 2026-09-04 - Three times, a bad test was mistaken for a bad product

Each of these looked like a defect and was not:

1. `pythonw` was reported as working because the diagnostic redirected its
   output, which gave it a real `sys.stdout` and hid the bug. The redirection
   was the flaw in the test.
2. A launcher was reported as importing the wrong copy of the package. The test
   ran with cwd inside the other project, and `sys.path[0]` is `''`.
3. The `sh` launcher was reported as failing through a symlink. Git Bash had
   silently made a copy, not a symlink; `test -L` said so.

**Pattern:** when the product looks broken, prove the harness first. Check what
the test actually did, not what it was meant to do. Two of these three were
caught only because the output was read carefully rather than skimmed for a
pass.

---

## 2026-09-04 - Adding a dependency can subtract a feature

Installing `tkinterdnd2` broke the window entirely. Its Tcl commands exist only
on a `TkinterDnD.Tk()` root, so `drop_target_register` raised on a plain root
and killed construction. All 14 GUI tests and any embedding application would
have gone with it.

**Pattern:** after adding a dependency, run the suite before believing the
feature works. And treat an optional enhancement as optional at runtime, not
just at import: wrap the activation, degrade, and say which state you are in.

---

## 2026-09-04 - Decoration ate the product, and only a screenshot caught it

Styling the checkbox indicators to match the Windows 95 palette set the selected
colour to the same `#FFFFFF` as the unselected one. Every ticked box rendered as
empty. The variables were correct; the window lied about them. "Keep backup"
decides whether the user's originals survive.

No test caught it. Looking at the screenshot did.

**Pattern:** for anything visual, look at it. And when restyling a control that
carries state, the assertion is that the states remain distinguishable, not that
the colours are pretty.

---

## 2026-09-04 - Writing docs through Python string literals corrupts paths

`"\bin"` is a backspace followed by `in`. This silently turned
`.tools\bin` into `.toolsin` in the README twice and in a memory index once,
and was caught the third time only by scanning the output for control
characters.

**Pattern:** use raw strings or `chr(92)` for Windows paths, and verify written
files by byte scan rather than by reading them back on screen. The corruption is
invisible in a terminal.
