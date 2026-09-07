#!/usr/bin/env python3
"""Validate local identity, asset, translation, and publish-sequence gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def image_info(path: Path) -> tuple[str, int, int]:
    with path.open("rb") as fh:
        head = fh.read(24)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            width, height = struct.unpack(">II", head[16:24])
            return "image/png", width, height
        if head[:2] != b"\xff\xd8":
            raise ValueError("unsupported or invalid image signature")
        fh.seek(2)
        while True:
            byte = fh.read(1)
            if not byte:
                break
            if byte != b"\xff":
                continue
            marker = fh.read(1)
            while marker == b"\xff":
                marker = fh.read(1)
            if marker in {bytes([x]) for x in range(0xC0, 0xC4)} | {bytes([x]) for x in range(0xC5, 0xC8)} | {bytes([x]) for x in range(0xC9, 0xCC)} | {bytes([x]) for x in range(0xCD, 0xD0)}:
                length = struct.unpack(">H", fh.read(2))[0]
                data = fh.read(length - 2)
                height, width = struct.unpack(">HH", data[1:5])
                return "image/jpeg", width, height
            length_bytes = fh.read(2)
            if len(length_bytes) != 2:
                break
            length = struct.unpack(">H", length_bytes)[0]
            fh.seek(length - 2, 1)
    raise ValueError("JPEG dimensions not found")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def artifact_hash(root: Path, paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p)):
        h.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(sha256(path).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def update_state(root: Path, value: str, current_hash: str) -> None:
    state_path = root / "执行状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema_version": "1.0", "product_id": root.name}
    prior_hash = state.get("artifact_sha256")
    later = {"ready_to_commit", "committing", "committed"}
    if prior_hash != current_hash or state.get("status") not in later:
        state["status"] = value
        state.pop("commit_token", None)
    state["artifact_sha256"] = current_hash
    stages = list(dict.fromkeys([*state.get("completed_stages", []), "image_audit_ready", "qa_passed"]))
    state["completed_stages"] = stages
    state["last_error"] = None
    state["next_action"] = "prepare Feishu commit"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(state_path, state)
    round_id = state.get("round_id")
    round_path = root.parent.parent / "产品批次" / f"{round_id}.json" if round_id else None
    if round_path and round_path.exists():
        round_doc = json.loads(round_path.read_text(encoding="utf-8"))
        for product in round_doc.get("products", []):
            if str(product.get("product_id")) == root.name:
                product["status"] = state["status"]
                product["phase"] = "qa"
                product["artifact_sha256"] = current_hash
        round_doc["updated_at"] = state["updated_at"]
        atomic_json(round_path, round_doc)


def mark_needs_rework(root: Path, errors: list[str]) -> None:
    state_path = root / "执行状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema_version": "1.1", "product_id": root.name}
    state["status"] = "needs_rework"
    state.pop("commit_token", None)
    state["last_error"] = errors[0] if errors else "product validation failed"
    state["next_action"] = "complete or refresh GPT in-app-browser image audit, then rerun QA"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(state_path, state)
    round_id = state.get("round_id")
    round_path = root.parent.parent / "产品批次" / f"{round_id}.json" if round_id else None
    if round_path and round_path.exists():
        round_doc = json.loads(round_path.read_text(encoding="utf-8"))
        for product in round_doc.get("products", []):
            if str(product.get("product_id")) == root.name:
                product["status"] = "needs_rework"
                product["phase"] = "image_audit"
                product.pop("commit_token", None)
        round_doc["updated_at"] = state["updated_at"]
        atomic_json(round_path, round_doc)


def pick(mapping: dict, *keys: str):
    for key in keys:
        if mapping.get(key) not in (None, ""):
            return mapping[key]
    return None


def approved_source(review: dict) -> bool:
    return (
        review.get("publish_decision") == "include"
        and review.get("product_variant_match") in {"exact_variant", "shared_product", "shared_approved"}
        and review.get("mixed_model_status") == "none"
        and review.get("text_language_judgment") in {
            "no_text", "english_only", "no_german_or_non_english_detected",
            "yes_german", "yes_non_english",
        }
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-dir", required=True, type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    root = args.product_dir.resolve()
    manifest_path = root / "图片资产清单.json"
    draft_path = root / "文案" / "文案底稿.json"
    translation_path = root / "英文翻译图片" / "图片翻译清单.json"
    audit_path = root / "图片审计" / "GPT图片审计记录.json"
    errors: list[str] = []
    notes: list[str] = []
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"gate": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "errors": [f"cannot load required JSON: {exc}"]}, ensure_ascii=False))
        return 12

    job = {}
    if (root / "产品任务.json").exists():
        job = json.loads((root / "产品任务.json").read_text(encoding="utf-8"))
    product = draft.get("product", {})
    job_variant = job.get("variant", {})
    manifest_vid = str(pick(manifest, "variant_id") or "")
    manifest_sku = str(pick(manifest, "sku") or "")
    draft_vid = str(pick(product, "variant_id") or pick(product.get("variant", {}) if isinstance(product.get("variant"), dict) else {}, "id") or "")
    draft_sku = str(pick(product, "sku") or pick(product.get("variant", {}) if isinstance(product.get("variant"), dict) else {}, "sku") or "")
    job_vid = str(job_variant.get("id", ""))
    job_sku = str(job_variant.get("sku", ""))
    vids = {x for x in (manifest_vid, draft_vid, job_vid) if x}
    skus = {x for x in (manifest_sku, draft_sku, job_sku) if x}
    check("variant_identity", all((manifest_vid, draft_vid, job_vid, manifest_sku, draft_sku, job_sku)) and len(vids) == 1 and len(skus) == 1, f"variant_ids={sorted(vids)}, skus={sorted(skus)}; all three documents must identify the variant")

    items = manifest.get("items", [])
    successful = [x for x in items if x.get("download_status") == "success"]
    positions = len(items)
    canonical = len({x.get("canonical_url") for x in items if x.get("canonical_url")})
    strict = len({x.get("sha256") for x in successful if x.get("sha256")})
    effective = int(manifest.get("effective_visual_count") or manifest.get("effective_visual_unique_count") or strict)
    check("asset_count_relation", positions >= canonical >= strict >= effective, f"positions={positions}, canonical={canonical}, strict={strict}, effective={effective}")

    missing_manifest_files = []
    corrupt_manifest_files = []
    unique_files = {}
    for item in successful:
        filename = item.get("filename")
        if not filename or not re.fullmatch(r"[a-fA-F0-9]{64}", str(item.get("sha256", ""))):
            corrupt_manifest_files.append(str(filename) + ": missing filename or full SHA-256")
            continue
        if filename in unique_files:
            continue
        unique_files[filename] = item
        path = root / "原始图片" / filename
        if not path.is_file() or path.stat().st_size == 0:
            missing_manifest_files.append(filename)
            continue
        try:
            image_info(path)
            if item.get("sha256") and sha256(path) != str(item["sha256"]).lower():
                corrupt_manifest_files.append(filename + ": hash mismatch")
        except Exception as exc:
            corrupt_manifest_files.append(filename + f": {exc}")
    check("source_files", not missing_manifest_files and not corrupt_manifest_files, f"missing={missing_manifest_files}, corrupt={corrupt_manifest_files}")

    audit_errors = []
    audit = {}
    audit_by_sha = {}
    translation_reviews_by_sha = {}
    if not audit_path.exists():
        audit_errors.append("图片审计/GPT图片审计记录.json missing")
    else:
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            if audit.get("analysis_source") != "gpt_in_app_browser_chatgpt":
                audit_errors.append("analysis_source is not gpt_in_app_browser_chatgpt")
            chat_url = str(audit.get("chatgpt_conversation_url", ""))
            if not chat_url.startswith("https://chatgpt.com/"):
                audit_errors.append("missing valid ChatGPT conversation URL")
            if job and job.get("image_analysis_mode") != "gpt_in_app_browser_chatgpt":
                audit_errors.append("产品任务.json does not require GPT in-app-browser image analysis")
            source_hashes = {str(x.get("sha256", "")).lower() for x in successful if x.get("sha256")}
            for item in audit.get("items", []):
                item_hash = str(item.get("sha256", "")).lower()
                if item_hash:
                    audit_by_sha[item_hash] = item
                required_fields = ("asset", "product_variant_match", "visual_duplicate_decision", "text_language_judgment", "mixed_model_status", "publish_decision", "reason")
                missing_fields = [field for field in required_fields if item.get(field) in (None, "")]
                if missing_fields:
                    audit_errors.append(f"audit item {item.get('asset')}: missing {missing_fields}")
            missing_hashes = sorted(source_hashes - set(audit_by_sha))
            if missing_hashes:
                audit_errors.append(f"source image audit coverage missing {len(missing_hashes)} hashes")
            for review in audit.get("translation_reviews", []):
                review_hash = str(review.get("sha256", "")).lower()
                if review_hash:
                    translation_reviews_by_sha[review_hash] = review
        except Exception as exc:
            audit_errors.append(f"invalid GPT image audit record: {exc}")
    check("gpt_in_app_browser_image_audit", not audit_errors, str(audit_errors))

    sequence = draft.get("image_plan", {}).get("recommended_order", [])
    ranks = [x.get("rank") for x in sequence]
    check("consecutive_ranks", ranks == list(range(1, len(sequence) + 1)) and bool(sequence), f"ranks={ranks}")
    invalid_subtitles = [x.get("rank") for x in sequence if not re.search(r"[\u3400-\u9fff]", str(x.get("subtitle", "")))]
    check("chinese_image_subtitles", not invalid_subtitles, f"invalid_ranks={invalid_subtitles}")
    banned = {Path(str(x.get("asset", ""))).name.casefold() for x in draft.get("image_plan", {}).get("do_not_use", [])}
    banned.update(Path(str(x.get("asset", "") if isinstance(x, dict) else x)).name.casefold() for x in job.get("excluded_assets", []))
    sequence_names = {Path(str(x.get("asset", ""))).name.casefold() for x in sequence}
    trace_names = {Path(str(x.get("source_asset", ""))).name.casefold() for x in sequence if x.get("source_asset")}
    check("excluded_assets", not (banned & (sequence_names | trace_names)), f"leaked={sorted(banned & (sequence_names | trace_names))}")
    invalid_paths = []
    invalid_images = []
    sequence_audit_errors = []
    for item in sequence:
        rel = str(item.get("asset", ""))
        path = (root / rel).resolve()
        if not rel or " OR " in rel.upper() or root not in path.parents or not path.is_file():
            invalid_paths.append(rel)
            continue
        try:
            image_info(path)
        except Exception as exc:
            invalid_images.append(f"{rel}: {exc}")
        if path.parent == (root / "原始图片").resolve():
            source_review = audit_by_sha.get(sha256(path), {})
            if not approved_source(source_review):
                sequence_audit_errors.append(f"rank {item.get('rank')}: source image is not approved for inclusion")
    check("publish_paths", not invalid_paths and not invalid_images, f"invalid_paths={invalid_paths}, invalid_images={invalid_images}")

    german_sources = {
        str(x.get("filename", "")).casefold()
        for x in items
        if audit_by_sha.get(str(x.get("sha256", "")).lower(), {}).get("text_language_judgment") in {"yes_german", "yes_non_english"}
    }
    untranslated = sorted(german_sources & sequence_names)
    check("localized_sequence", not untranslated, f"untranslated_sources={untranslated}")

    translation_errors = []
    translation_bindings = {}
    if translation_path.exists():
        translations = json.loads(translation_path.read_text(encoding="utf-8"))
        for item in translations.get("items", []):
            source = (translation_path.parent / str(item.get("source", ""))).resolve()
            output = (translation_path.parent / str(item.get("output", ""))).resolve()
            if root not in source.parents or root not in output.parents or not source.is_file():
                translation_errors.append("translation source/output must be inside the product and source must exist")
                continue
            if source == output:
                translation_errors.append(f"source equals output: {source}")
            if not output.is_file():
                translation_errors.append(f"missing output: {output}")
                continue
            try:
                image_info(output)
                output_hash = sha256(output)
                recorded_hash = str(pick(item, "output_sha256", "sha256") or "").lower()
                source_hash = sha256(source)
                if source_hash != str(item.get("input_sha256", "")).lower():
                    translation_errors.append(f"missing or stale input SHA-256: {source.name}")
                if output_hash != recorded_hash:
                    translation_errors.append(f"hash mismatch: {output.name}")
                if source.name.casefold() in banned or not approved_source(audit_by_sha.get(source_hash, {})):
                    translation_errors.append(f"translation source not approved: {source.name}")
                if output in translation_bindings:
                    translation_errors.append(f"duplicate translation output: {output.name}")
                translation_bindings[output] = source
                review = translation_reviews_by_sha.get(output_hash, {})
                if str(review.get("status", "")).upper() != "PASS":
                    translation_errors.append(f"missing passed GPT review: {output.name}")
                if review.get("residual_non_english_text") is not False:
                    translation_errors.append(f"residual-language review not passed: {output.name}")
                if str(review.get("fidelity_status", "")).upper() != "PASS":
                    translation_errors.append(f"fidelity review not passed: {output.name}")
            except Exception as exc:
                translation_errors.append(f"invalid output {output.name}: {exc}")
    for item in sequence:
        output = (root / str(item.get("asset", ""))).resolve()
        if output.parent == (root / "原始图片").resolve():
            continue
        source = translation_bindings.get(output)
        if source is None:
            translation_errors.append(f"unregistered publish output: {item.get('asset')}")
        elif not item.get("source_asset") or (root / str(item["source_asset"])).resolve() != source:
            translation_errors.append(f"publish source_asset differs from translation manifest: {item.get('asset')}")
    check("translation_outputs", not translation_errors, str(translation_errors))

    sequence_review = audit.get("final_sequence_review", {}) if isinstance(audit, dict) else {}
    expected_sequence_hash = hashlib.sha256(canonical_json(sequence).encode("utf-8")).hexdigest()[:12]
    if str(sequence_review.get("status", "")).upper() != "PASS":
        sequence_audit_errors.append("final sequence has no passed GPT review")
    if sequence_review.get("sequence_sha256") != expected_sequence_hash:
        sequence_audit_errors.append("final sequence hash differs from GPT-reviewed sequence")
    check("gpt_final_sequence_review", not sequence_audit_errors, str(sequence_audit_errors))

    publish_pointer = root / "最终发布图片" / "当前发布清单.json"
    publish_errors = []
    publish_manifest = None
    if not publish_pointer.exists():
        publish_errors.append("最终发布图片/当前发布清单.json missing; run 生成最终发布图片.py")
    else:
        try:
            publish_manifest = json.loads(publish_pointer.read_text(encoding="utf-8"))
            publish_dir = Path(str(publish_manifest.get("directory", ""))).resolve()
            expected_root = (root / "最终发布图片").resolve()
            if publish_dir != expected_root and expected_root not in publish_dir.parents:
                publish_errors.append("publish directory escapes product 最终发布图片")
            pitems = publish_manifest.get("items", [])
            if len(pitems) != len(sequence) or publish_manifest.get("count") != len(sequence):
                publish_errors.append("publish manifest count differs from final sequence")
            for seq, item in zip(sequence, pitems):
                if item.get("rank") != seq.get("rank") or item.get("source_asset") != seq.get("asset"):
                    publish_errors.append(f"rank {seq.get('rank')}: publish manifest no longer matches draft sequence")
                    continue
                target = (publish_dir / str(item.get("final_filename", ""))).resolve()
                if target.parent != publish_dir:
                    publish_errors.append("final filename escapes publish directory")
                    continue
                if not target.is_file():
                    publish_errors.append(f"missing final asset: {target.name}")
                    continue
                image_info(target)
                source = (root / str(seq.get("asset", ""))).resolve()
                if not item.get("sha256") or sha256(target) != str(item["sha256"]).lower() or not source.is_file() or sha256(source) != str(item["sha256"]).lower():
                    publish_errors.append(f"final asset hash mismatch: {target.name}")
        except Exception as exc:
            publish_errors.append(f"invalid publish manifest: {exc}")
    check("final_publish_assets", not publish_errors, str(publish_errors))

    part_files = [str(p.relative_to(root)) for p in root.rglob("*.part")]
    zero_files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.stat().st_size == 0]
    check("temporary_and_zero_files", not part_files and not zero_files, f"part={part_files}, zero={zero_files}")

    status = "PASS" if not errors else "FAIL"
    artifact_paths = [manifest_path, draft_path]
    if (root / "产品任务.json").exists():
        artifact_paths.append(root / "产品任务.json")
    if audit_path.exists():
        artifact_paths.append(audit_path)
    if translation_path.exists():
        artifact_paths.append(translation_path)
    if publish_pointer.exists():
        artifact_paths.append(publish_pointer)
    current_hash = artifact_hash(root, artifact_paths)
    report = {"schema_version": "1.0", "status": status, "product_dir": str(root), "artifact_sha256": current_hash, "checks": checks, "errors": errors, "notes": notes}
    output = args.output or root / "质量检查" / "自动校验报告.json"
    atomic_json(output, report)
    if status == "PASS":
        update_state(root, "qa_passed", current_hash)
    else:
        mark_needs_rework(root, errors)
    print(json.dumps({"status": status, "gate_count": len(checks), "failures": len(errors), "output": str(output)}, ensure_ascii=False))
    return 0 if status == "PASS" else 12


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
