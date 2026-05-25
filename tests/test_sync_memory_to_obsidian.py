from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-memory-to-obsidian.py"
SPEC = importlib.util.spec_from_file_location("sync_memory_to_obsidian", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["sync_memory_to_obsidian"] = sync
SPEC.loader.exec_module(sync)


def source(path: Path, name: str, description: str = "desc") -> sync.SourceMemory:
    text = f"""---
name: {name}
description: {description}
metadata:
  type: feedback
---

body
"""
    fm, body = sync.parse_frontmatter(text)
    return sync.SourceMemory(path=path, name=name, text=text, frontmatter=fm, body=body)


class SyncMemoryToObsidianTests(unittest.TestCase):
    def test_repo_memory_slug_matches_claude_project_path_format(self) -> None:
        self.assertEqual(
            sync.repo_memory_slug(Path("/Users/stephan.merk/Python_Code/jarvis")),
            "-Users-stephan-merk-Python-Code-jarvis",
        )

    def test_parse_frontmatter_handles_yaml_block_scalars(self) -> None:
        text = """---
name: foo
description: |
  This is a multi-line
  description
metadata:
  type: feedback
---
Body
"""
        fm, body = sync.parse_frontmatter(text)

        self.assertEqual(fm["name"], "foo")
        self.assertEqual(fm["metadata"]["type"], "feedback")
        self.assertIn("multi-line", fm["description"])
        self.assertEqual(body, "Body\n")

    def test_collect_vault_memories_skips_corrupt_utf8_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.md").write_bytes(b"\xff\xfe\x00")
            (root / "good.md").write_text("---\naliases:\n  - ok\n---\nbody", encoding="utf-8")

            memories = sync.collect_vault_memories(root)

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].aliases, ("ok",))

    def test_sync_sources_skips_sanitized_filename_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            sources = [
                source(knowledge / "one.md", "foo bar"),
                source(knowledge / "two.md", "foo/bar"),
            ]

            created, updated, skipped = sync.sync_sources(
                sources=sources,
                vault_memories=[],
                knowledge_dir=knowledge,
                force_update=False,
                dry_run=False,
            )

            self.assertEqual((created, updated, skipped), (1, 0, 1))
            self.assertEqual(len(list(knowledge.glob("*.md"))), 1)

    def test_existing_target_without_alias_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            target = knowledge / "foo_bar.md"
            target.write_text("manual", encoding="utf-8")

            created, updated, skipped = sync.sync_sources(
                sources=[source(knowledge / "one.md", "foo bar")],
                vault_memories=[],
                knowledge_dir=knowledge,
                force_update=False,
                dry_run=False,
            )

            self.assertEqual((created, updated, skipped), (0, 0, 1))
            self.assertEqual(target.read_text(encoding="utf-8"), "manual")

    def test_orphan_marking_only_touches_managed_stubs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed.md"
            curated = root / "curated.md"
            managed.write_text(
                "---\naliases:\n  - gone\nsync_managed: true\norphaned: false\n---\nbody",
                encoding="utf-8",
            )
            curated.write_text("---\naliases:\n  - gone-too\n---\nbody", encoding="utf-8")
            memories = sync.collect_vault_memories(root)

            marked = sync.mark_orphans(memories, active_names=set(), dry_run=False)

            managed_fm, _ = sync.parse_frontmatter(managed.read_text(encoding="utf-8"))
            curated_fm, _ = sync.parse_frontmatter(curated.read_text(encoding="utf-8"))

        self.assertEqual(marked, 1)
        self.assertIs(managed_fm["orphaned"], True)
        self.assertNotIn("orphaned", curated_fm)

    def test_force_update_only_overwrites_managed_alias_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            managed = knowledge / "old.md"
            managed.write_text(
                "---\naliases:\n  - foo\nsync_managed: true\norphaned: false\n---\nold body",
                encoding="utf-8",
            )
            memories = sync.collect_vault_memories(knowledge)

            created, updated, skipped = sync.sync_sources(
                sources=[source(knowledge / "foo.md", "foo")],
                vault_memories=memories,
                knowledge_dir=knowledge,
                force_update=True,
                dry_run=False,
            )

            self.assertEqual((created, updated, skipped), (0, 1, 0))
            self.assertIn("Ursprünglicher Memory-Inhalt", managed.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
