from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from skill_router.catalog import load_skill, scan_roots
from skill_router.search import search_skills


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "python-debug").mkdir()
        (self.root / "python-debug" / "SKILL.md").write_text(
            "---\nname: python-debug\ndescription: Debug Python tracebacks and failing tests.\n---\n\n# Debug\n\nRead the traceback.\n",
            encoding="utf-8",
        )
        (self.root / "react-ui").mkdir()
        (self.root / "react-ui" / "SKILL.md").write_text(
            "---\nname: react-ui\ndescription: Build React user interfaces.\n---\n\n# UI\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_scans_and_loads_skill(self) -> None:
        skills = scan_roots([self.root])
        self.assertEqual([skill.skill_id for skill in skills], ["python-debug", "react-ui"])
        skill = load_skill("python-debug", [self.root])
        self.assertEqual(skill.name, "python-debug")
        self.assertIn("Read the traceback", skill.body)

    def test_search_ranks_matching_skill(self) -> None:
        results = search_skills(scan_roots([self.root]), "debug a Python traceback", limit=2)
        self.assertEqual(results[0].skill.skill_id, "python-debug")
        self.assertEqual(len(results), 1)

    def test_search_can_abstain(self) -> None:
        self.assertEqual(search_skills(scan_roots([self.root]), "manage a payroll", limit=3), [])

    def test_load_does_not_accept_a_path(self) -> None:
        with self.assertRaises(KeyError):
            load_skill("../python-debug", [self.root])


if __name__ == "__main__":
    unittest.main()

