"""Synthetic images only; no product visual judgments or browser operations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("screen", Path(__file__).resolve().parents[1] / "scripts/准备主图初筛.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = {"product_id": "fixture", "product_url": "https://example.com/p", "variant": {"id": "v1", "sku": "s1"}}
        S.save(self.root / "产品任务.json", self.job)
        S.save(self.root / "文案/文案底稿.json", {"image_plan": {"required_roles": ["外观"]}})
        (self.root / "原始图片").mkdir()
        items = []
        for i in range(66):
            path = self.root / "原始图片" / f"{i}.png"
            S.Image.new("RGB", (20, 20), (i, 50, 100)).save(path)
            items.append({"section": "gallery", "section_position": i + 1, "filename": path.name, "download_status": "success", "sha256": S.file_hash(path)})
        self.manifest = {"gallery_inventory": {"expected_count": 66}, "items": items}
        S.save(self.root / "图片资产清单.json", self.manifest)

    def test_66_images_need_two_overview_requests_with_complete_mapping(self):
        plan = S.prepare(self.root)
        self.assertEqual((plan["unique_images"], plan["sheet_count"], len(plan["batches"])), (66, 6, 2))
        rows = [x for b in plan["batches"] for sheet in b["sheets"] for x in sheet["items"]]
        self.assertEqual(len({x["sha256"] for x in rows}), 66)
        self.assertFalse((self.root / "图片审计/GPT图片审计记录.json").exists())
        self.assertFalse((self.root / "执行状态.json").exists())

    def test_unchanged_plan_and_sheets_are_reused(self):
        S.prepare(self.root)
        folder = self.root / "图片审计/初筛准备"
        before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        S.prepare(self.root)
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in folder.iterdir()})

    def test_matching_context_reuses_only_resolved_hash_bound_results(self):
        plan = S.prepare(self.root)
        results = [{"sha256": x["sha256"], "decision": "exclude", "reason": "synthetic only"} for x in self.manifest["items"][:60]]
        S.save(self.root / "图片审计/GPT图片审计记录.json", {"analysis_source": "gpt_in_app_browser_chatgpt", "chatgpt_conversation_url": "https://chatgpt.com/c/synthetic", "gallery_screening_context_sha256": plan["review_context_sha256"], "gallery_screening": results})
        next_plan = S.prepare(self.root)
        self.assertEqual((len(next_plan["reused_images"]), next_plan["pending_images"]), (60, 6))
        self.job["variant"]["sku"] = "changed"
        S.save(self.root / "产品任务.json", self.job)
        self.assertEqual(S.prepare(self.root)["pending_images"], 66)

    def test_stale_source_blocks_preparation(self):
        (self.root / "原始图片/0.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            S.prepare(self.root)

    def test_no_role_definition_blocks_preparation(self):
        S.save(self.root / "文案/文案底稿.json", {"image_plan": {}})
        with self.assertRaisesRegex(ValueError, "required_roles"):
            S.prepare(self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
