"""回归测试（纯 stdlib unittest，零依赖）。

覆盖本次精读修复的高风险点：
- sync 闸门（无 final/无提案/错章/无审校注记/未登记实体）
- dry-run 与正式同步的合并错误等价性
- init --clean 保留审计记录
- export --txt 同章多版本去重
- 章号/版本排序（v10 > v2）
- count_aliases 空别名安全
- 快照回滚清理新增顶层状态文件

运行：`python -m unittest discover -s tests -v`
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import checks, cli, common, evidence, snapshot, state  # noqa: E402


def _run(argv: list[str]) -> int:
    return cli.main(argv)


def _init_book(root: Path, name: str = "书") -> Path:
    book = root / name
    _run(["init", "-w", str(book), "-t", "回归书", "-g", "都市", "-p", "主角"])
    return book


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stage_chapter(book: Path, ch: str = "ch_001", review: bool = True) -> None:
    """写 beats/raw/final/审校注记，满足 review_gate 的前置形状。"""
    n = ch.split("_")[1]
    _write(book / "outlines" / "vol_01" / "beats" / f"{ch}.md", f"""---
chapter: {ch}
vol: vol_01
form: 单场景章
pov: 主角·贴身
words: 2200-4500
style_notes: 短促 | 直入冲突 | 强钩
---
## 拍点
- 主角回家。
## 验收
1. 主角是否回家
""")
    _write(book / "manuscript" / "vol_01" / "raw" / f"{ch}_v1.md", "主角回家。\n")
    _write(book / "manuscript" / "vol_01" / "final" / f"{ch}.md", "主角回家。\n")
    if review:
        _write(book / "log" / "review" / f"{ch}.md",
               "## 验收\n1. ✓ 正文第一句「主角回家。」明确写出主角已回家（证据行足够长）。\n")


def _proposal(book: Path, ch: str = "ch_001", op: str = "ch_001.syncer.test",
              **extra) -> None:
    data = {
        "schema": "novel-studio.state-mutation/v2",
        "chapter": ch,
        "operation_id": op,
        "current": {"location": "家", "present_characters": ["主角"]},
        "entities": [{"action": "upsert", "name": "主角", "type": "person", "summary": "主角"}],
        "synopsis": {"title": "回家", "text": "主角回家。"},
    }
    data.update(extra)
    _write(book / "state" / "inbox" / f"{ch}.json", json.dumps(data, ensure_ascii=False))


class SyncGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.book = _init_book(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_final_blocks_sync(self) -> None:
        _stage_chapter(self.book, review=False)
        _proposal(self.book)
        (self.book / "manuscript" / "vol_01" / "final" / "ch_001.md").unlink()
        rc = _run(["sync", "-w", str(self.book), "ch_001", "--dry-run"])
        self.assertEqual(rc, 1)

    def test_wrong_proposal_chapter_blocks_sync(self) -> None:
        _stage_chapter(self.book)
        _proposal(self.book, ch="ch_002", op="ch_002.syncer.test")
        rc = _run(["sync", "-w", str(self.book), "ch_001", "--dry-run"])
        self.assertEqual(rc, 1)

    def test_missing_review_note_blocks_sync(self) -> None:
        _stage_chapter(self.book, review=False)
        _proposal(self.book)
        rc = _run(["sync", "-w", str(self.book), "ch_001", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertTrue(checks.review_gate(self.book, "ch_001"))

    def test_unregistered_character_blocks_snapshot(self) -> None:
        _stage_chapter(self.book)
        _proposal(self.book, current={"location": "家", "present_characters": ["幽灵"]})
        rc = _run(["sync", "-w", str(self.book), "ch_001"])
        self.assertEqual(rc, 1)
        self.assertEqual(snapshot.list_snapshots(self.book), [])


class DryRunEquivalenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.book = _init_book(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_error_caught_in_dry_run(self) -> None:
        _stage_chapter(self.book)
        _proposal(self.book, entities=[{"action": "retire", "name": "不存在的人"}])
        dry = _run(["sync", "-w", str(self.book), "ch_001", "--dry-run"])
        real = _run(["sync", "-w", str(self.book), "ch_001"])
        self.assertEqual(dry, 1)
        self.assertEqual(real, 1)

    def test_existing_pool_initial_is_locked(self) -> None:
        _stage_chapter(self.book)
        extra = {"ledger": {"pools": {"standard_currency": {"initial": 999}},
                            "transactions": []}}
        _proposal(self.book, **extra)
        self.assertEqual(_run(["sync", "-w", str(self.book), "ch_001", "--dry-run"]), 1)


class CleanPreservesAuditTest(unittest.TestCase):
    def test_processed_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            book = _init_book(Path(td))
            audit = book / "state" / "inbox" / "processed" / "ch_001.json"
            _write(audit, '{"audit": true}\n')
            _write(book / "manuscript" / "vol_01" / "raw" / "ch_001_v1.md", "x\n")
            rc = _run(["init", "-w", str(book), "--clean"])
            self.assertEqual(rc, 0)
            self.assertTrue(audit.exists())


class ExportAndSortTest(unittest.TestCase):
    def test_export_dedupes_latest_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            book = _init_book(Path(td), "导出书")
            _write(book / "manuscript" / "vol_01" / "final" / "ch_001.md", "v1正文\n")
            _write(book / "manuscript" / "vol_01" / "final" / "ch_001_v2.md", "v2正文\n")
            _write(book / "manuscript" / "vol_01" / "final" / "ch_001_v10.md", "v10正文\n")
            self.assertEqual(_run(["export", "-w", str(book), "--txt"]), 0)
            out = (book / "export" / "回归书.txt").read_text(encoding="utf-8")
            self.assertEqual(out.count("正文"), 1)
            self.assertIn("v10正文", out)

    def test_version_sort_and_alias_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            book = _init_book(Path(td))
            _write(book / "manuscript" / "vol_01" / "final" / "ch_001_v2.md", "v2\n")
            _write(book / "manuscript" / "vol_01" / "final" / "ch_001_v10.md", "v10\n")
            got = evidence.final_chapters(book)
            self.assertEqual(got[0][2], "v10\n")
            self.assertEqual(evidence.count_aliases("测试", []), {})
            self.assertEqual(evidence.count_aliases("测试", [""]), {})


class SnapshotRollbackCleanupTest(unittest.TestCase):
    def test_new_state_file_removed_on_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            book = _init_book(Path(td))
            _stage_chapter(book)
            _proposal(book)
            self.assertEqual(_run(["sync", "-w", str(book), "ch_001"]), 0)
            extra = book / "state" / "extra.json"
            _write(extra, '{"extra": 1}\n')
            rc = _run(["snapshot", "rollback", "-w", str(book), "ch_001_done"])
            self.assertEqual(rc, 0)
            self.assertFalse(extra.exists())


if __name__ == "__main__":
    unittest.main()
