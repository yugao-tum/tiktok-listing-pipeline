#!/usr/bin/env python3
"""Prepare numbered overview sheets and a delta queue; never decide image quality."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, doc):
    if path.is_file() and read(path) == doc:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prepare(root, images_per_sheet=12, sheets_per_request=3):
    root = root.resolve()
    if not 1 <= images_per_sheet <= 12 or not 1 <= sheets_per_request <= 5:
        raise ValueError("use 1..12 images per sheet and 1..5 sheets per request")
    job = read(root / "产品任务.json", {})
    draft = read(root / "文案/文案底稿.json", {})
    roles = draft.get("image_plan", {}).get("required_roles", [])
    variant = job.get("variant") or {}
    if not variant.get("id") or not variant.get("sku") or not roles:
        raise ValueError("fix target variant and required_roles before preparing screening")
    manifest = read(root / "图片资产清单.json", {})
    gallery = [x for x in manifest.get("items", []) if x.get("section") == "gallery"]
    if not gallery or manifest.get("gallery_inventory", {}).get("expected_count") != len(gallery):
        raise ValueError("gallery inventory incomplete")
    requirements_path = root / "图片审计/购买信息需求.json"
    # Sibling queue changes invalidate family QA, not this unchanged leaf's visual evidence.
    context = digest({"policy": "gallery-screening-v3", "product_id": job.get("product_id", root.name), "product_url": job.get("product_url"), "variant": variant, "excluded_variants": job.get("excluded_variants", []), "excluded_assets": job.get("excluded_assets", []), "required_roles": roles, "requirements_sha256": file_hash(requirements_path) if requirements_path.is_file() else None})
    unique = {}
    for item in gallery:
        path = (root / "原始图片" / str(item.get("filename") or "")).resolve()
        key = str(item.get("sha256") or "").lower()
        if item.get("download_status") != "success" or path.parent != (root / "原始图片").resolve() or not path.is_file() or file_hash(path) != key:
            raise ValueError(f"missing or changed gallery file: {item.get('section_position')}")
        record = unique.setdefault(key, {"asset": path.relative_to(root).as_posix(), "sha256": key, "positions": [], "variant_scopes": []})
        record["positions"].append(item.get("section_position"))
        scope = item.get("variant_scope", "unknown")
        if scope not in record["variant_scopes"]:
            record["variant_scopes"].append(scope)
    audit = read(root / "图片审计/GPT图片审计记录.json", {})
    reusable = audit.get("gallery_screening_context_sha256") == context and audit.get("analysis_source") == "gpt_in_app_browser_chatgpt" and str(audit.get("chatgpt_conversation_url", "")).startswith("https://chatgpt.com/")
    cached = {str(x.get("sha256", "")).lower(): x for x in audit.get("gallery_screening", [])} if reusable else {}
    pending = []
    reused = []
    for index, (key, record) in enumerate(unique.items(), 1):
        row = {"label": f"G{index:03d}", **record}
        old = cached.get(key, {})
        if old.get("decision") in {"adopt", "exclude"} and str(old.get("reason") or "").strip():
            reused.append(row)
        else:
            pending.append(row)
    output = root / "图片审计/初筛准备"
    output.mkdir(parents=True, exist_ok=True)
    sheets = []
    for offset in range(0, len(pending), images_per_sheet):
        group = pending[offset:offset + images_per_sheet]
        sheet_id = digest({"layout": "3-columns-512x416-label24-v1", "items": group})[:16]
        path = output / f"主图总览_{sheet_id}.jpg"
        metadata = path.with_suffix(".json")
        previous = read(metadata, {})
        if not path.is_file() or previous.get("sha256") != file_hash(path):
            rows = (len(group) + 2) // 3
            canvas = Image.new("RGB", (1536, rows * 416), "white")
            draw = ImageDraw.Draw(canvas)
            font = ImageFont.load_default(size=24)
            for index, row in enumerate(group):
                with Image.open(root / row["asset"]) as source:
                    thumb = ImageOps.exif_transpose(source).convert("RGB")
                    thumb.thumbnail((492, 366))
                x, y = index % 3 * 512, index // 3 * 416
                canvas.paste(thumb, (x + (512 - thumb.width) // 2, y + (376 - thumb.height) // 2))
                draw.text((x + 12, y + 381), f"{row['label']}  SHA {row['sha256'][:12]}", fill="black", font=font)
            tmp = path.with_suffix(".jpg.tmp")
            canvas.save(tmp, format="JPEG", quality=90)
            os.replace(tmp, path)
        sheet = {"asset": path.relative_to(root).as_posix(), "sha256": file_hash(path), "items": group}
        save(metadata, sheet)
        sheets.append(sheet)
    plan = {"schema_version": "1.0", "review_context_sha256": context, "product_id": job.get("product_id", root.name), "gallery_positions": len(gallery), "unique_images": len(unique), "reused_images": reused, "pending_images": len(pending), "sheet_count": len(sheets), "batches": [{"batch_id": f"screen-{i // sheets_per_request + 1:02d}", "sheets": sheets[i:i + sheets_per_request]} for i in range(0, len(sheets), sheets_per_request)], "limitations": "Overview sheets support first-pass screening only. Unreadable text or uncertain variant needs original-image inspection; adopted images still need detailed source review. No audit verdict or execution state is written."}
    save(output / "初筛计划.json", plan)
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-dir", type=Path, required=True)
    ap.add_argument("--images-per-sheet", type=int, default=12)
    ap.add_argument("--sheets-per-request", type=int, default=3)
    args = ap.parse_args()
    plan = prepare(args.product_dir, args.images_per_sheet, args.sheets_per_request)
    print(json.dumps({"plan": str(args.product_dir.resolve() / "图片审计/初筛准备/初筛计划.json"), "unique_images": plan["unique_images"], "pending_images": plan["pending_images"], "reused_images": len(plan["reused_images"]), "batches": len(plan["batches"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
