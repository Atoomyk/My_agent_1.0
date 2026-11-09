from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_agent.diff_view import (
    DiffRow,
    build_rows,
    change_kind,
    change_stats,
    format_fragments,
    format_selection,
    format_size,
    format_stats,
    normalize_text,
    plural,
    row_line,
    text_bytes,
)
from project_agent.editor_tabs import EditorTabs
from project_agent.hunks import merge_hunks, split_hunks
from project_agent.secrets import SecretVault


class SizeAndPluralTests(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(0), "0 Б")
        self.assertEqual(format_size(381), "381 Б")
        self.assertEqual(format_size(1023), "1023 Б")
        self.assertEqual(format_size(1024), "1.0 КБ")
        self.assertEqual(format_size(4300), "4.2 КБ")
        self.assertEqual(format_size(1024 * 1024), "1.0 МБ")

    def test_plural_and_fragments(self):
        self.assertEqual(plural(1, "фрагмент", "фрагмента", "фрагментов"), "фрагмент")
        self.assertEqual(plural(3, "фрагмент", "фрагмента", "фрагментов"), "фрагмента")
        self.assertEqual(plural(5, "фрагмент", "фрагмента", "фрагментов"), "фрагментов")
        self.assertEqual(plural(11, "фрагмент", "фрагмента", "фрагментов"), "фрагментов")
        self.assertEqual(plural(21, "фрагмент", "фрагмента", "фрагментов"), "фрагмент")
        self.assertEqual(format_fragments(1), "1 фрагмент")
        self.assertEqual(format_fragments(3), "3 фрагмента")
        self.assertEqual(format_fragments(5), "5 фрагментов")

    def test_text_bytes_ignores_line_endings(self):
        self.assertEqual(text_bytes("a\nb\n"), 4)
        self.assertEqual(text_bytes("a\r\nb\r\n"), 4)
        self.assertEqual(text_bytes(None), 0)


class ChangeKindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(change_kind("", "x\n"), "новый файл")
        self.assertEqual(change_kind("x\n", ""), "очищен")
        self.assertEqual(change_kind("a\n", "b\n"), "изменён")
        self.assertEqual(change_kind("a\n", "a\n"), "без изменений")

    def test_crlf_only_change_is_not_a_rewrite(self):
        before = "a\r\nb\r\n"
        after = "a\nb\n"
        self.assertEqual(normalize_text(before), after)
        self.assertEqual(change_kind(before, after), "без изменений")
        stats = change_stats(before, after)
        self.assertEqual(stats.hunks, 0)
        self.assertEqual(format_stats(stats), "без изменений")


class ChangeStatsTests(unittest.TestCase):
    def test_modified_file(self):
        stats = change_stats("a\nb\nc\n", "a\nB\nc\n")
        self.assertEqual(stats.kind, "изменён")
        self.assertEqual(stats.plus, 1)
        self.assertEqual(stats.minus, 1)
        self.assertEqual(stats.hunks, 1)
        self.assertEqual(stats.accepted_hunks, 1)
        self.assertFalse(stats.partial)
        self.assertEqual(format_stats(stats), "изменён · +1 −1 · 1 фрагмент · 6 Б → 6 Б")

    def test_size_delta_in_summary(self):
        stats = change_stats("a\n", "abc\n")
        self.assertEqual(format_stats(stats), "изменён · +1 −1 · 1 фрагмент · 2 Б → 4 Б (+2 Б)")

    def test_new_and_cleared_files(self):
        created = change_stats("", "x\ny\n")
        self.assertEqual(created.kind, "новый файл")
        self.assertEqual(created.plus, 2)
        self.assertEqual(created.minus, 0)
        self.assertIn("новый файл · +2 −0 · 1 фрагмент", format_stats(created))
        cleared = change_stats("x\n", "")
        self.assertEqual(cleared.kind, "очищен")
        self.assertEqual(cleared.plus, 0)
        self.assertEqual(cleared.minus, 1)

    def test_selection_line(self):
        before = "a\nb\nc\nd\ne\n"
        after = "A\nb\nc\nd\nE\n"
        everything = change_stats(before, after)
        self.assertEqual(everything.hunks, 2)
        self.assertEqual(everything.plus, 2)
        self.assertEqual(everything.minus, 2)
        self.assertEqual(format_selection(everything), "")

        half = change_stats(before, after, {0})
        self.assertTrue(half.partial)
        self.assertEqual(half.accepted_hunks, 1)
        self.assertEqual(half.selected_plus, 1)
        self.assertEqual(half.selected_minus, 1)
        self.assertEqual(format_selection(half), "выбрано: +1 −1 · 1 из 2 фрагментов")

        nothing = change_stats(before, after, set())
        self.assertTrue(nothing.nothing)
        self.assertEqual(format_selection(nothing), "выбрано: ничего")


class BuildRowsTests(unittest.TestCase):
    def test_numbering_and_kinds(self):
        rows = build_rows("a\nb\nc\n", "a\nB\nc\n")
        self.assertEqual([row.kind for row in rows], ["ctx", "hunk", "del", "add", "ctx"])
        self.assertEqual((rows[0].old_no, rows[0].new_no, rows[0].text), (1, 1, "a"))
        self.assertEqual(rows[1].text, "@@ -2 +2 @@")
        self.assertEqual((rows[2].old_no, rows[2].new_no, rows[2].text), (2, None, "b"))
        self.assertEqual((rows[3].old_no, rows[3].new_no, rows[3].text), (None, 2, "B"))
        self.assertEqual((rows[4].old_no, rows[4].new_no, rows[4].text), (3, 3, "c"))

    def test_hunk_index_and_accept_state(self):
        before = "a\nb\nc\nd\ne\n"
        after = "A\nb\nc\nd\nE\n"
        rows = build_rows(before, after, accepted={0})
        headers = [row for row in rows if row.kind == "hunk"]
        self.assertEqual([row.hunk_index for row in headers], [0, 1])
        self.assertTrue(headers[0].accepted)
        self.assertFalse(headers[1].accepted)
        rejected = [row for row in rows if row.hunk_index == 1 and row.kind in {"add", "del"}]
        self.assertTrue(rejected)
        self.assertTrue(all(not row.accepted for row in rejected))

    def test_limit_appends_skip_row(self):
        rows = build_rows("a\nb\nc\n", "a\nB\nc\n", limit=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1].kind, "skip")
        self.assertEqual(rows[-1].text, "…")

    def test_long_equal_run_collapses(self):
        before = "\n".join(f"l{index}" for index in range(20))
        after = before.replace("l10", "changed")
        rows = build_rows(before, after)
        self.assertIn("skip", [row.kind for row in rows])
        self.assertLess(len(rows), 20)

    def test_empty_and_trailing_newline_cases(self):
        created = build_rows("", "x\n")
        self.assertEqual([row.kind for row in created], ["hunk", "add"])
        self.assertEqual(created[1].new_no, 1)

        cleared = build_rows("x\n", "")
        self.assertEqual([row.kind for row in cleared], ["hunk", "del"])
        self.assertEqual(cleared[1].old_no, 1)

        no_trailing = build_rows("a", "a\nb")
        self.assertEqual(no_trailing[-1].kind, "add")
        self.assertEqual(no_trailing[-1].text, "b")

    def test_crlf_gives_no_rows(self):
        rows = build_rows("a\r\nb\r\n", "a\nb\n")
        self.assertEqual([row.kind for row in rows], ["ctx", "ctx"])

    def test_row_line(self):
        row = DiffRow("hunk", None, None, "@@ -2 +2 @@", hunk_index=0, accepted=True)
        self.assertEqual(row_line(row), "@@ -2 +2 @@  ✓ принят")
        rejected = DiffRow("hunk", None, None, "@@ -2 +2 @@", hunk_index=0, accepted=False)
        self.assertEqual(row_line(rejected), "@@ -2 +2 @@  ✗ отклонён")
        added = DiffRow("add", None, 7, "x = 1")
        self.assertEqual(row_line(added), "        7 + x = 1")


class EditorDraftTests(unittest.TestCase):
    def test_open_draft_marks_dirty_without_duplicate_tab(self):
        tabs = EditorTabs()
        index = tabs.open_draft("a.py", "disk\n", "draft\n")
        self.assertEqual(index, 0)
        tab = tabs.active()
        self.assertIsNotNone(tab)
        self.assertEqual(tab.saved, "disk\n")
        self.assertEqual(tab.text, "draft\n")
        self.assertTrue(tab.dirty)
        self.assertTrue(tab.label().endswith("●"))

        again = tabs.open_draft("a.py", "disk\n", "draft 2\n")
        self.assertEqual(again, 0)
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs.active().text, "draft 2\n")


class PartialAcceptWithSecretsTests(unittest.TestCase):
    def test_partial_merge_restores_secrets(self):
        vault = SecretVault()
        secret = 'token = "s3cr3t-value-12345"'
        before = f"line1\n{secret}\nline3\n"
        after = f"line1\n{secret}\nline3\nline4\n"
        hidden_before = vault.redact(before)
        hidden_after = vault.redact(after)
        self.assertNotIn("s3cr3t-value-12345", hidden_before)
        self.assertEqual(hidden_before.count("[[SEC:"), 1)

        hunks = split_hunks(hidden_before, hidden_after)
        self.assertEqual(len(hunks), 1)
        merged = merge_hunks(hidden_before, hidden_after, {0})
        self.assertEqual(vault.restore(merged), after)

        rejected = merge_hunks(hidden_before, hidden_after, set())
        self.assertEqual(vault.restore(rejected), before)

    def test_partial_accept_keeps_rejected_fragments(self):
        vault = SecretVault()
        secret = 'token = "s3cr3t-value-12345"'
        before = f"a\n{secret}\nc\nd\ne\n"
        after = f"A\n{secret}\nc\nd\nE\n"
        hidden_before = vault.redact(before)
        hidden_after = vault.redact(after)
        hunks = split_hunks(hidden_before, hidden_after)
        self.assertEqual(len(hunks), 2)

        merged = merge_hunks(hidden_before, hidden_after, {1})
        self.assertEqual(vault.restore(merged), f"a\n{secret}\nc\nd\nE\n")
        stats = change_stats(hidden_before, hidden_after, {1})
        self.assertEqual(stats.accepted_hunks, 1)
        self.assertIn("1 из 2 фрагментов", format_selection(stats))


if __name__ == "__main__":
    unittest.main()
