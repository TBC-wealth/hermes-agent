"""Regression tests for the rg/grep error guard in content search.

The guard in ``_search_with_rg`` / ``_search_with_grep`` had two defects on
``origin/main`` (see PR replacing #39710):

1. **Unreachable on a hard error.** Both methods pipe the search through
   ``| head`` with no ``pipefail``, so the pipeline reported head's exit code
   (0), masking rg/grep's error code (2). The guard never fired, and the
   error text — merged into stdout by ``_exec`` (``stderr=subprocess.STDOUT``)
   — was parsed as bogus match lines instead of being surfaced.

2. **Would have nuked partial results if it ever did fire.** A broad
   ``exit_code == 2`` check discards real matches whenever rg/grep also hit a
   non-fatal error (e.g. one unreadable file in a tree that otherwise
   matched), which both tools signal with exit 2.

The fix adds ``set -o pipefail`` so the real exit code propagates, splits
tool diagnostics from match output by *shape*, and only surfaces an error
when exit==2 AND no usable match payload remains.

These tests drive the real methods through the real local terminal backend.
"""

import json
import os
import shutil

import pytest

from tools.file_operations import (
    ShellFileOperations,
    _is_valid_regex,
    _pattern_has_regex_newline,
    _split_tool_diagnostics,
)
from tools.environments.local import LocalEnvironment
from tools.file_tools import search_tool


def _ops(root):
    return ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))


@pytest.fixture
def match_tree(tmp_path):
    """A tree with several files all containing 'needle'."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"needle line {i}\n")
    return tmp_path


@pytest.fixture
def partial_error_tree(tmp_path):
    """A tree with matches plus one unreadable file (forces exit 2 + matches)."""
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text(f"needle line {i}\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    locked = sub / "locked.txt"
    locked.write_text("needle in locked\n")
    os.chmod(locked, 0o000)
    yield tmp_path
    os.chmod(locked, 0o755)  # let pytest clean up tmp_path


# Run every test once per available backend method.
_METHODS = ["_search_with_grep"]
if shutil.which("rg"):
    _METHODS.append("_search_with_rg")


def _search(ops, method, pattern, path, **kw):
    fn = getattr(ops, method)
    return fn(pattern, str(path), kw.get("file_glob"), kw.get("limit", 50),
              kw.get("offset", 0), kw.get("output_mode", "content"),
              kw.get("context", 0))


@pytest.mark.parametrize("method", _METHODS)
class TestSearchErrorGuard:
    def test_happy_path_returns_matches(self, method, match_tree):
        res = _search(_ops(match_tree), method, "needle", match_tree)
        assert res.error is None
        assert len(res.matches) == 5

    def test_hard_error_is_surfaced(self, method, match_tree):
        # An invalid regex makes rg/grep exit 2 with only diagnostics in
        # stdout. The guard MUST surface it — not return empty matches.
        #
        # rg's engine additionally rejects some patterns Python's `re`
        # accepts (e.g. backreferences), so `_search_with_rg` uses one of
        # those here — the #117 fixed-string fallback only engages for
        # patterns invalid under Python's `re`, and must not intercept a
        # genuine rg-only regex error.
        pattern = r"(a)\1" if method == "_search_with_rg" else "["
        res = _search(_ops(match_tree), method, pattern, match_tree)
        assert res.error is not None, "search error was silently swallowed"
        assert "Search failed" in res.error
        assert not res.matches


    def test_count_mode_with_partial_error(self, method, partial_error_tree):
        res = _search(_ops(partial_error_tree), method, "needle",
                      partial_error_tree, output_mode="count")
        assert res.error is None
        assert res.total_count >= 4


class TestSearchContentNewlineWarning:
    def test_odd_backslash_n_is_detected_as_regex_newline(self):
        assert _pattern_has_regex_newline(r"needle\n")
        assert _pattern_has_regex_newline(r"needle\\\n")


    def test_literal_backslash_n_pattern_does_not_warn(self, match_tree):
        res = _ops(match_tree).search(
            r"absent\\npattern",
            path=str(match_tree),
            target="content",
        )

        assert res.error is None
        assert res.total_count == 0
        assert res.warning is None


class TestSplitToolDiagnostics:
    """Unit coverage for the shape-based diagnostic/payload splitter."""

    def test_pure_error_has_empty_payload(self):
        out = "rg: regex parse error:\n    (?:[)\n       ^\nerror: unclosed character class\n"
        diagnostics, payload = _split_tool_diagnostics(out)
        assert payload.strip() == ""
        assert "regex parse error" in diagnostics


    def test_context_lines_and_separator_are_payload(self):
        out = "a.py:5:hit\na.py-6-after\n--\nb.py:9:hit\n"
        diagnostics, payload = _split_tool_diagnostics(out)
        assert diagnostics == ""
        assert "--" in payload
        assert "a.py-6-after" in payload


# ---------------------------------------------------------------------------
# Fixed-string fallback for invalid-regex patterns (#117)
#
# `search_files pattern="*One Development*"` used to hard-error with rg's
# "regex parse error: repetition operator missing expression" (a leading
# `*` has nothing to repeat) — one wasted round-trip before the caller could
# retry. `_search_with_rg` now validates the pattern locally with Python's
# `re` first and, when it doesn't compile, searches it as a literal fixed
# string (rg --fixed-strings) in the same call instead of erroring.
# ---------------------------------------------------------------------------

class TestIsValidRegex:
    """Unit coverage for the local pre-validation helper."""

    def test_leading_glob_star_is_invalid_regex(self):
        assert not _is_valid_regex("*One Development*")

    def test_unclosed_bracket_is_invalid_regex(self):
        assert not _is_valid_regex("[")

    def test_ordinary_regex_is_valid(self):
        assert _is_valid_regex("needle")
        assert _is_valid_regex(r"O.e Development")


@pytest.mark.skipif(not shutil.which("rg"), reason="requires rg (ripgrep)")
class TestFixedStringFallback:
    @pytest.fixture
    def glob_pattern_tree(self, tmp_path):
        """A tree with a file whose content literally contains asterisks
        around the phrase, reproducing the exact bench case from #117
        (e.g. markdown emphasis: '*One Development*')."""
        (tmp_path / "notes.md").write_text("See *One Development* for details.\n")
        (tmp_path / "other.md").write_text("Nothing relevant here.\n")
        return tmp_path

    def test_invalid_regex_falls_back_to_literal_match(self, glob_pattern_tree):
        res = _ops(glob_pattern_tree)._search_with_rg(
            "*One Development*", str(glob_pattern_tree), None, 50, 0, "content", 0
        )
        assert res.error is None
        assert res.total_count == 1
        assert "notes.md" in res.matches[0].path
        assert "not valid regex" in (res.warning or "")

    def test_invalid_regex_via_search_files_tool(self, glob_pattern_tree):
        # Full stack through the actual search_files tool entrypoint,
        # matching the exact call reported in #117.
        raw = search_tool(pattern="*One Development*", path=str(glob_pattern_tree))
        result = json.loads(raw)
        assert "error" not in result
        assert result["total_count"] == 1

    def test_invalid_regex_with_no_literal_match_is_clean(self, glob_pattern_tree):
        # No file contains this literal string either — a real regex-invalid
        # pattern with no fixed-string interpretation should behave
        # sensibly: a plain zero-result search, not an rg error.
        res = _ops(glob_pattern_tree)._search_with_rg(
            "*Two Development*", str(glob_pattern_tree), None, 50, 0, "content", 0
        )
        assert res.error is None
        assert res.total_count == 0

    def test_valid_regex_still_searched_as_regex(self, glob_pattern_tree):
        # A well-formed regex must keep matching as a regex (no behavior
        # change for callers who intended regex syntax) — `.` here matches
        # the 'n' in "One", which a literal fixed-string search would not.
        res = _ops(glob_pattern_tree)._search_with_rg(
            r"O.e Development", str(glob_pattern_tree), None, 50, 0, "content", 0
        )
        assert res.error is None
        assert res.total_count == 1
        assert "not valid regex" not in (res.warning or "")
