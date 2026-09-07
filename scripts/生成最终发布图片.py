#!/usr/bin/env python3
"""Create a versioned publish view named from final rank and image subtitle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path


INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_title(value: str, limit: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = INVALID.sub("_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "图片")


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-dir", required=True, type=Path)
    ap.add_argument("--draft", type=Path)
    ap.add_argument("--output-root", type=Path)
    args = ap.parse_args()

    root = args.product_dir.resolve()
    draft_path = args.draft or root / "文案" / "文案底稿.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    sequence = draft.get("image_plan", {}).get("recommended_order", [])
    if not sequence:
        raise SystemExit("draft has no image_plan.recommended_order")
    ranks = [x.get("rank") for x in sequence]
    if ranks != list(range(1, len(sequence) + 1)):
        raise SystemExit("image ranks must be consecutive and start at 1")
    for item in sequence:
        asset = str(item.get("asset", ""))
        if not asset or " OR " in asset.upper():
            raise SystemExit(f"rank {item.get('rank')}: asset must be one concrete path")
        subtitle = str(item.get("subtitle", ""))
        if not subtitle or not re.search(r"[\u3400-\u9fff]", subtitle):
            raise SystemExit(f"rank {item.get('rank')}: missing meaningful Chinese subtitle")

    version = hashlib.sha256(canonical_json(sequence).encode("utf-8")).hexdigest()[:12]
    output_root = (args.output_root or root / "最终发布图片").resolve()
    output_dir = output_root / version
    output_dir.mkdir(parents=True, exist_ok=True)
    names: set[str] = set()
    manifest_items = []
    for item in sequence:
        source = (root / str(item["asset"])).resolve()
        if root not in source.parents or not source.is_file():
            raise SystemExit(f"rank {item['rank']}: missing or escaping asset {item['asset']}")
        ext = source.suffix.lower()
        title = safe_title(str(item["subtitle"]))
        filename = f"{item['rank']:02d}_{title}{ext}"
        if filename.casefold() in names:
            filename = f"{item['rank']:02d}_{title}_{sha256(source)[:8]}{ext}"
        names.add(filename.casefold())
        target = output_dir / filename
        source_hash = sha256(source)
        if target.exists():
            if sha256(target) != source_hash:
                raise SystemExit(f"refusing to overwrite different file: {target}")
        else:
            shutil.copy2(source, target)
        manifest_items.append({
            "rank": item["rank"],
            "subtitle": item["subtitle"],
            "source_asset": str(item["asset"]),
            "trace_source_asset": item.get("source_asset"),
            "final_filename": filename,
            "sha256": source_hash,
            "bytes": target.stat().st_size,
        })
    manifest = {"schema_version": "1.0", "sequence_sha256": version, "count": len(manifest_items), "directory": str(output_dir), "items": manifest_items}
    atomic_json(output_dir / "发布图片清单.json", manifest)
    atomic_json(output_root / "当前发布清单.json", manifest)
    print(json.dumps({"ok": True, "count": len(manifest_items), "version": version, "directory": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
