"""Verify whole product gallery capture with mocked network; never fetch real products."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("collector", Path(__file__).resolve().parents[1] / "scripts" / "采集Shopify产品素材.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


class CollectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="listing-collection-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        C.atomic_json(self.root / "产品任务.json", {"product_url": "https://example.com/products/test", "variant": {"id": "v1", "sku": "s1"}})
        C.atomic_json(self.root / "执行状态.json", {"status": "queued"})
        self.product = {"id": 1, "variants": [{"id": "v1", "sku": "s1"}], "images": ["https://example.com/a.png", "https://example.com/b.png"]}
        buf = io.BytesIO()
        C.Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
        self.png = buf.getvalue()
        self.session = MagicMock()
        self.session.headers = {}
        self.session.get.side_effect = self.response
        self.addCleanup(patch.stopall)
        patch.object(C.requests, "Session", return_value=self.session).start()
        patch.object(C, "build_contact_sheets", return_value=[]).start()

    def response(self, url, **kwargs):
        data = self.png if url.endswith(".png") else json.dumps(self.product).encode()
        return SimpleNamespace(content=data, text="<html></html>", json=lambda: self.product,
                               headers={"Content-Type": "image/png"}, raise_for_status=lambda: None)

    def image_requests(self):
        return [call.args[0] for call in self.session.get.call_args_list if call.args[0].endswith(".png")]

    def test_default_discovers_without_download_or_manifest_overwrite(self):
        existing = {"sentinel": "preserve"}
        C.atomic_json(self.root / "图片资产清单.json", existing)
        result = C.collect(self.root, discovery_only=True)
        self.assertEqual(result["image_downloads"], 0)
        self.assertEqual(self.image_requests(), [])
        self.assertEqual(json.loads((self.root / "图片资产清单.json").read_text(encoding="utf-8")), existing)

    def test_only_selected_position_downloaded_and_retry_reuses_file(self):
        C.collect(self.root, [2])
        self.assertEqual(self.image_requests(), ["https://example.com/b.png"])
        self.session.get.reset_mock()
        C.collect(self.root, [2])
        self.assertEqual(self.image_requests(), [])

    def test_invalid_position_does_not_download(self):
        with self.assertRaisesRegex(ValueError, "unknown candidate"):
            C.collect(self.root, [99])
        self.assertEqual(self.image_requests(), [])

    def test_all_images_requires_explicit_mode(self):
        self.product["body_html"] = '<img src="https://example.com/detail.png">'
        C.collect(self.root, all_images=True)
        self.assertEqual(len(self.image_requests()), 3)

    def test_default_downloads_full_gallery_but_not_description(self):
        self.product["body_html"] = '<img src="https://example.com/detail.png">'
        result = C.collect(self.root)
        self.assertEqual(self.image_requests(), ["https://example.com/a.png", "https://example.com/b.png"])
        self.assertEqual(result["collection_mode"], "gallery_all")
        self.assertEqual(result["gallery_inventory"]["expected_count"], 2)
        self.assertEqual(result["gallery_inventory"]["downloaded_count"], 2)
        # Identical bytes at different gallery positions share a single source file.
        self.assertEqual(len(list((self.root / "原始图片").iterdir())), 1)
        self.session.get.reset_mock()
        C.collect(self.root)
        self.assertEqual(self.image_requests(), [])

    def test_gallery_download_does_not_pre_exclude_other_variant(self):
        self.product["images"][1] = {"src": "https://example.com/b.png", "variant_ids": ["other"]}
        C.collect(self.root)
        self.assertEqual(len(self.image_requests()), 2)
        manifest = json.loads((self.root / "图片资产清单.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"][1]["variant_scope"], "other_variant")

    def sibling(self):
        self.product["variants"].append({"id": "v2", "sku": "s2"})
        self.product["images"][0] = {"src": "https://example.com/a.png", "variant_ids": ["v1"]}
        C.collect(self.root)
        target = self.root / "sibling"
        C.atomic_json(target / "产品任务.json", {"product_url": "https://example.com/products/test", "variant": {"id": "v2", "sku": "s2"}})
        C.atomic_json(target / "执行状态.json", {"status": "queued"})
        self.session.get.reset_mock()
        return target

    def test_sibling_reuses_bytes_but_recalculates_variant_scope(self):
        target = self.sibling()
        result = C.collect(target, reuse_from=[self.root])
        self.assertEqual(self.image_requests(), [])
        self.assertEqual(result["collection_stats"]["reused_sibling_urls"], 2)
        self.assertEqual(result["gallery_inventory"]["downloaded_count"], 2)
        manifest = json.loads((target / "图片资产清单.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"][0]["variant_scope"], "other_variant")
        self.assertEqual(manifest["visual_review_status"], "pending")
        self.assertFalse((target / "图片审计/GPT图片审计记录.json").exists())

    def test_changed_official_snapshot_prevents_sibling_reuse(self):
        target = self.sibling()
        self.product["title"] = "changed official data"
        result = C.collect(target, reuse_from=[self.root])
        self.assertEqual(len(self.image_requests()), 2)
        self.assertEqual(result["collection_stats"]["reused_sibling_urls"], 0)

    def test_damaged_donor_falls_back_to_download(self):
        target = self.sibling()
        for path in (self.root / "原始图片").iterdir():
            path.write_bytes(b"damaged")
        C.collect(target, reuse_from=[self.root])
        self.assertEqual(len(self.image_requests()), 2)

    def test_wrong_product_donor_not_reused(self):
        target = self.sibling()
        path = self.root / "图片资产清单.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["product_url"] = "https://example.com/products/other"
        C.atomic_json(path, doc)
        C.collect(target, reuse_from=[self.root])
        self.assertEqual(len(self.image_requests()), 2)

    def test_collection_does_not_generate_duplicate_overviews_by_default(self):
        C.collect(self.root)
        C.build_contact_sheets.assert_not_called()
        C.collect(self.root, contact_sheets=True)
        C.build_contact_sheets.assert_called_once()

    def test_missing_gallery_does_not_fall_back_to_detail_images(self):
        self.product["images"] = []
        self.product["body_html"] = '<img src="https://example.com/detail.png">'
        with self.assertRaisesRegex(ValueError, "gallery inventory"):
            C.collect(self.root)
        self.assertEqual(self.image_requests(), [])

    def test_failed_gallery_position_remains_incomplete_and_retry_is_targeted(self):
        def fail_one(url, **kwargs):
            if url.endswith("b.png"):
                raise RuntimeError("synthetic failure")
            return self.response(url, **kwargs)
        self.session.get.side_effect = fail_one
        result = C.collect(self.root)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["gallery_inventory"]["downloaded_count"], 1)
        self.session.get.reset_mock()
        self.session.get.side_effect = self.response
        result = C.collect(self.root)
        self.assertEqual(self.image_requests(), ["https://example.com/b.png"])
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
