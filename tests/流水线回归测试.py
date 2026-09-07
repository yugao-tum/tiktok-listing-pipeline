"""Isolated mechanical fixtures; synthetic audit entries are not production evidence."""

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII=")


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAYLOAD = load_script("生成飞书录入载荷")
PLAN = load_script("规划图片翻译批次")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="listing-regression-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / "产品工作区" / "fixture"
        self.original = self.root / "原始图片" / "source.png"
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(PNG)
        self.job = {"product_id": "fixture", "variant": {"id": "v1", "sku": "s1"}, "image_analysis_mode": "gpt_in_app_browser_chatgpt"}
        self.manifest = {"variant_id": "v1", "sku": "s1", "items": [{"filename": "source.png", "sha256": sha(self.original), "download_status": "success", "canonical_url": "https://example.com/source.png"}]}
        self.draft = {"product": {"variant_id": "v1", "sku": "s1"}, "copy": {"chinese_title": "测试椅", "english_title": "Test chair", "english_bullets": ["Synthetic fixture"], "detail_page": {"opening": {"heading": "Test", "body": "Synthetic only"}}}, "image_plan": {"recommended_order": [{"rank": 1, "asset": "原始图片/source.png", "subtitle": "产品主图"}], "do_not_use": []}}
        self.audit = {"analysis_source": "gpt_in_app_browser_chatgpt", "chatgpt_conversation_url": "https://chatgpt.com/c/synthetic-test-only", "prompt_version": "synthetic-fixture", "items": [self.review("source.png", sha(self.original))], "translation_reviews": []}
        self.save()

    def review(self, asset, digest):
        return {"asset": asset, "sha256": digest, "product_variant_match": "exact_variant", "visual_duplicate_decision": "unique", "text_language_judgment": "no_text", "mixed_model_status": "none", "publish_decision": "include", "reason": "synthetic test only"}

    def run_script(self, name, *args):
        # Reproduce an English Windows runner; CLI output must still be UTF-8.
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        return subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), *map(str, args)], capture_output=True, text=True, encoding="utf-8", env=env)

    def save(self):
        seq = self.draft["image_plan"]["recommended_order"]
        seq_hash = hashlib.sha256(json.dumps(seq, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
        self.audit["final_sequence_review"] = {"status": "PASS", "sequence_sha256": seq_hash}
        for relative, data in [("产品任务.json", self.job), ("图片资产清单.json", self.manifest), ("文案/文案底稿.json", self.draft), ("图片审计/GPT图片审计记录.json", self.audit)]:
            write(self.root / relative, data)

    def prepare(self):
        self.save()
        result = self.run_script("生成最终发布图片", "--product-dir", self.root)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def validate(self, expected=0):
        result = self.run_script("校验产品发布包", "--product-dir", self.root)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return read(self.root / "质量检查/自动校验报告.json")

    def ready(self):
        self.prepare()
        self.validate()
        write(self.root / "质量检查/质量检查报告.json", {"status": "PASS", "draft_sha256": sha(self.root / "文案/文案底稿.json")})

    def translate(self):
        target = self.root / "英文翻译图片" / "translated.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(PNG + b"synthetic-output")
        self.audit["items"][0]["text_language_judgment"] = "yes_non_english"
        self.audit["translation_reviews"] = [{"asset": target.name, "sha256": sha(target), "status": "PASS", "fidelity_status": "PASS", "residual_non_english_text": False}]
        self.draft["image_plan"]["recommended_order"][0].update(asset="英文翻译图片/translated.png", source_asset="原始图片/source.png")
        translation = {"items": [{"source": "../原始图片/source.png", "output": target.name, "input_sha256": sha(self.original), "output_sha256": sha(target)}]}
        write(target.parent / "图片翻译清单.json", translation)
        return target.parent / "图片翻译清单.json"

    def test_local_package_and_payload_pass(self):
        self.ready()
        values, meta = PAYLOAD.build_values(self.root, True)
        self.assertIn("QA：PASS", values["素材与处理状态"])
        self.assertEqual(meta["artifact_sha256"], read(self.root / "执行状态.json")["artifact_sha256"])

    def test_translation_pass(self):
        self.translate()
        self.ready()
        PAYLOAD.build_values(self.root, True)

    def test_missing_identity_fails(self):
        self.draft["product"] = {}
        self.prepare()
        self.assertIn("variant_identity", str(self.validate(12)["errors"]))

    def test_unregistered_file_fails(self):
        path = self.root / "other" / "rogue.png"
        path.parent.mkdir()
        path.write_bytes(PNG)
        self.draft["image_plan"]["recommended_order"][0]["asset"] = "other/rogue.png"
        self.prepare()
        self.assertIn("unregistered publish output", str(self.validate(12)["errors"]))

    def test_excluded_source_fails(self):
        self.job["excluded_assets"] = ["source.png"]
        self.prepare()
        self.assertIn("excluded_assets", str(self.validate(12)["errors"]))

    def test_excluded_translation_fails(self):
        self.translate()
        self.job["excluded_assets"] = ["source.png"]
        self.prepare()
        self.assertIn("translation source not approved", str(self.validate(12)["errors"]))

    def test_uncertain_source_fails(self):
        self.audit["items"][0]["product_variant_match"] = "uncertain_requires_review"
        self.prepare()
        self.assertIn("not approved for inclusion", str(self.validate(12)["errors"]))

    def test_missing_input_hash_fails(self):
        path = self.translate()
        doc = read(path)
        del doc["items"][0]["input_sha256"]
        write(path, doc)
        self.prepare()
        self.assertIn("input SHA-256", str(self.validate(12)["errors"]))

    def test_excluded_foreign_image_does_not_need_translation(self):
        other = self.original.with_name("excluded.png")
        other.write_bytes(PNG + b"excluded")
        self.manifest["items"].append({"filename": other.name, "sha256": sha(other), "download_status": "success", "canonical_url": "https://example.com/excluded.png"})
        review = self.review(other.name, sha(other))
        review.update(publish_decision="exclude", text_language_judgment="yes_non_english")
        self.audit["items"].append(review)
        self.prepare()
        self.validate()

    def test_stale_source_blocks_payload(self):
        self.ready()
        self.original.write_bytes(PNG + b"changed")
        with self.assertRaisesRegex(ValueError, "changed final"):
            PAYLOAD.build_values(self.root, True)

    def test_stale_job_blocks_payload(self):
        self.ready()
        self.job["excluded_assets"] = ["source.png"]
        write(self.root / "产品任务.json", self.job)
        with self.assertRaisesRegex(ValueError, "snapshot has changed"):
            PAYLOAD.build_values(self.root, True)

    def test_copy_qa_requires_current_draft_hash(self):
        self.ready()
        write(self.root / "质量检查/质量检查报告.json", {"status": "PASS"})
        with self.assertRaisesRegex(ValueError, "copy QA"):
            PAYLOAD.build_values(self.root, True)

    def test_preview_has_no_commit_shape_or_pass_claim(self):
        self.prepare()
        records = self.workspace / "records.json"
        output = self.workspace / "preview.json"
        write(records, {"fixture": "rec_fixture"})
        result = self.run_script("生成飞书录入载荷", "--workspace", self.workspace, "--records", records, "--output", output, "--allow-without-qa")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = read(output)
        self.assertNotIn("update_records", doc)
        self.assertNotIn("QA：PASS", str(doc))
        self.assertFalse(read(self.workspace / "preview_提交信息.json")["committable"])
        self.assertFalse((self.root / "执行状态.json").exists())

    def test_conflicting_qa_cannot_pass(self):
        self.assertFalse(PAYLOAD.qa_passed({"status": "FAIL", "verdict": "PASS"}))
        self.assertFalse(PAYLOAD.qa_passed({"status": "completed"}))

    def test_consumer_copy_drops_internal_notes(self):
        result = PAYLOAD.render_detail({"size_and_package": {"confirmed": [{"field": "Height", "value": "10 cm"}], "tbc_do_not_publish": ["SECRET_TBC"]}, "notes": ["INTERNAL_NOTE"], "consumer_notes": ["Assembly required"]})
        self.assertNotIn("SECRET_TBC", result)
        self.assertNotIn("INTERNAL_NOTE", result)
        self.assertIn("Assembly required", result)

    def test_batch_single_remainder_then_terminal(self):
        path = self.workspace / "plan.json"
        write(path, {"batches": [{"batch_id": "b001", "level": 5, "status": "failed", "items": [{"status": "success"}] * 4 + [{"status": "failed"}]}]})
        result = self.run_script("规划图片翻译批次", "degrade", "--plan", path, "--batch-id", "b001", "--reason", "synthetic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(read(path)["batches"][-1]["items"]), 1)
        result = self.run_script("规划图片翻译批次", "degrade", "--plan", path, "--batch-id", "b002", "--reason", "synthetic")
        self.assertEqual(result.returncode, 9)

    def test_completed_batch_not_failed(self):
        path = self.workspace / "plan.json"
        write(path, {"batches": [{"batch_id": "b001", "level": 1, "status": "running", "items": [{"status": "success"}]}]})
        result = self.run_script("规划图片翻译批次", "degrade", "--plan", path, "--batch-id", "b001", "--reason", "observed completion")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read(path)["batches"][0]["status"], "success")

    def test_duplicate_record_mapping_rejected(self):
        records = self.workspace / "records.json"
        write(records, {"fixture": "rec_same", "other": "rec_same"})
        result = self.run_script("生成飞书录入载荷", "--workspace", self.workspace, "--records", records, "--output", self.workspace / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same record ID", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
