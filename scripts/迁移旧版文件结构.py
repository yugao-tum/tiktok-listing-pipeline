#!/usr/bin/env python3
"""Safely migrate legacy English artifact names to the Chinese naming convention."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(value: str) -> str:
    value = INVALID.sub("_", value.strip())
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value or "图片"


def atomic_text(path: Path, value: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def move(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    if target.exists():
        raise RuntimeError(f"目标已存在，停止迁移: {target}")
    source.rename(target)
    return True


def move_tree_contents(source: Path, target: Path) -> int:
    """Move files from a Windows-locked directory while leaving empty shells."""
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(source.rglob("*"), key=lambda p: (len(p.parts), str(p))):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        moved += int(move(path, destination))
    return moved


def replace_in_text_files(root: Path, replacements: list[tuple[str, str]]) -> int:
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".md", ".csv"}:
            continue
        content = path.read_text(encoding="utf-8")
        updated = content
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated != content:
            atomic_text(path, updated)
            changed += 1
    return changed


def rename_images(product_root: Path) -> list[tuple[str, str]]:
    draft_path = product_root / "文案" / "文案底稿.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {}
    sequence = draft.get("image_plan", {}).get("recommended_order", [])
    labels: dict[str, tuple[int, str, bool]] = {}
    for item in sequence:
        subtitle = str(item.get("subtitle", "图片"))
        asset = Path(str(item.get("asset", ""))).name
        if asset:
            labels[asset] = (int(item.get("rank") or 0), subtitle, False)
        source = Path(str(item.get("source_asset", ""))).name
        if source:
            labels[source] = (int(item.get("rank") or 0), subtitle, True)

    replacements: list[tuple[str, str]] = []
    for folder, suffix in ((product_root / "原始图片", "原始图"), (product_root / "英文翻译图片", "英文图")):
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() in {".json", ".jsonl", ".csv", ".md"}:
                continue
            match = re.match(r"(\d{2})", path.stem)
            prefix = match.group(1) if match else "00"
            if path.name in labels:
                rank, subtitle, is_source_for_translation = labels[path.name]
                prefix = f"{rank:02d}" if rank else prefix
                detail = "待翻译原图" if is_source_for_translation else suffix
                new_name = f"{prefix}_{safe_title(subtitle)}_{detail}{path.suffix.lower()}"
            else:
                new_name = f"{prefix}_商品{suffix}{path.suffix.lower()}"
            target = path.with_name(new_name)
            if target != path:
                if target.exists():
                    raise RuntimeError(f"图片目标已存在，停止迁移: {target}")
                old_name = path.name
                path.rename(target)
                replacements.append((old_name, new_name))
    return replacements


def rename_publish_files(product_root: Path) -> int:
    publish_root = product_root / "最终发布图片"
    draft_path = product_root / "文案" / "文案底稿.json"
    if not publish_root.exists() or not draft_path.exists():
        return 0
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    labels = {int(x["rank"]): str(x["subtitle"]) for x in draft.get("image_plan", {}).get("recommended_order", [])}
    manifests = sorted(publish_root.rglob("发布图片清单.json"))
    pointer = publish_root / "当前发布清单.json"
    if pointer.exists():
        manifests.append(pointer)
    changed = 0
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        directory = Path(str(manifest.get("directory", "")))
        for item in manifest.get("items", []):
            rank = int(item.get("rank") or 0)
            subtitle = labels.get(rank, str(item.get("subtitle") or "图片"))
            old_name = str(item.get("final_filename", ""))
            extension = Path(old_name).suffix.lower()
            new_name = f"{rank:02d}_{safe_title(subtitle)}{extension}"
            source = directory / old_name
            target = directory / new_name
            if source.exists() and source != target:
                if target.exists():
                    raise RuntimeError(f"发布图片目标已存在，停止迁移: {target}")
                source.rename(target)
            item["subtitle"] = subtitle
            item["final_filename"] = new_name
        atomic_json(manifest_path, manifest)
        changed += 1
    return changed


def migrate_product(product_root: Path) -> dict:
    directory_moves = {
        "source_images": "原始图片",
        "translated_images": "英文翻译图片",
        "publish_images": "最终发布图片",
        "copy": "文案",
        "qa": "质量检查",
    }
    root_files = {
        "job.json": "产品任务.json",
        "state.json": "执行状态.json",
        "events.jsonl": "执行事件.jsonl",
        "asset_manifest.json": "图片资产清单.json",
        "asset_manifest.csv": "图片资产清单.csv",
        "evidence.jsonl": "证据记录.jsonl",
        "translation_plan.json": "图片翻译计划.json",
    }
    nested_files = {
        "英文翻译图片/translation_manifest.json": "英文翻译图片/图片翻译清单.json",
        "最终发布图片/current_manifest.json": "最终发布图片/当前发布清单.json",
        "文案/draft.json": "文案/文案底稿.json",
        "文案/draft.md": "文案/文案底稿.md",
        "质量检查/report.json": "质量检查/质量检查报告.json",
        "质量检查/report.md": "质量检查/质量检查报告.md",
        "质量检查/script_validation.json": "质量检查/自动校验报告.json",
    }
    moved = 0
    for old, new in directory_moves.items():
        source = product_root / old
        target = product_root / new
        if source.is_dir() and target.is_dir():
            moved += move_tree_contents(source, target)
        else:
            moved += int(move(source, target))
    for old, new in {**root_files, **nested_files}.items():
        moved += int(move(product_root / old, product_root / new))
    for path in (product_root / "最终发布图片").glob("*/publish_manifest.json") if (product_root / "最终发布图片").exists() else []:
        moved += int(move(path, path.with_name("发布图片清单.json")))

    replacements = [
        ("source_images", "原始图片"),
        ("translated_images", "英文翻译图片"),
        ("publish_images", "最终发布图片"),
        ("copy/draft.json", "文案/文案底稿.json"),
        ("qa/script_validation.json", "质量检查/自动校验报告.json"),
        ("qa/report", "质量检查/质量检查报告"),
        ("\\work\\", "\\产品工作区\\"),
        ("/work/", "/产品工作区/"),
    ]
    replacements.extend(rename_images(product_root))
    changed_text = replace_in_text_files(product_root, replacements)
    changed_manifests = rename_publish_files(product_root)
    return {"product_id": product_root.name, "moved": moved, "updated_text_files": changed_text, "updated_publish_manifests": changed_manifests}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, type=Path)
    args = ap.parse_args()
    workspace = args.workspace.resolve()
    for old_name, new_name in (("work", "产品工作区"), ("rounds", "产品批次")):
        source = workspace / old_name
        target = workspace / new_name
        if source.exists() and target.exists():
            children = sorted(source.iterdir())
            if not children:
                continue
        else:
            children = []
        try:
            if not children:
                move(source, target)
                continue
            raise PermissionError("merge locked legacy container")
        except PermissionError:
            # Windows may hold a directory handle from a prior agent even though
            # its ACL and attributes are normal. Move children without deleting
            # the locked legacy container.
            target.mkdir(parents=True, exist_ok=True)
            for child in children or sorted(source.iterdir()):
                destination = target / child.name
                if child.is_dir() and destination.is_dir():
                    move_tree_contents(child, destination)
                    continue
                try:
                    move(child, destination)
                except PermissionError:
                    if not child.is_dir():
                        raise
                    move_tree_contents(child, destination)
    product_root = workspace / "产品工作区"
    results = [migrate_product(path) for path in sorted(product_root.iterdir()) if path.is_dir()] if product_root.exists() else []
    print(json.dumps({"ok": True, "products": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
