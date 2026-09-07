#!/usr/bin/env python3
"""Export a portable, current listing folder after QA; never create an archive."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil


spec = importlib.util.spec_from_file_location("listing_payload", Path(__file__).with_name("生成飞书录入载荷.py"))
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)
MANIFEST = "交付清单.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def child(folder: Path, relative: str) -> Path:
    path = folder / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"invalid delivery path: {relative}")
    if folder not in path.resolve().parents:
        raise ValueError(f"delivery path escapes folder: {relative}")
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in [path, *path.parents] if p != folder and folder in p.parents):
        raise ValueError(f"linked delivery path is not supported: {relative}")
    return path


def prepare(root: Path, output_root: Path) -> dict:
    root = root.resolve()
    # Read-only validation; no Feishu payload, state transition or remote write.
    values, meta = payload.build_values(root, True, allow_committed=True)
    publish = json.loads((root / "最终发布图片/当前发布清单.json").read_text(encoding="utf-8"))
    folder = output_root / root.name
    if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
        raise ValueError(f"linked product delivery folder: {folder}")
    folder = folder.resolve()
    if folder == root or root in folder.parents or folder in root.parents:
        raise ValueError("delivery must be separate from the product evidence workspace")
    content = "\n\n".join(f"## {key}\n\n{values[key]}" for key in ("中文标题", "英文标题", "英文卖点", "完整详情页"))
    content += "\n\n## 图片顺序\n\n" + "\n".join(f"{x['rank']}. 图片/{x['final_filename']}" for x in publish["items"]) + "\n"
    data = content.encode("utf-8")
    desired = {"上架文案.md": {"sha256": digest(data), "data": data}}
    hashes = set()
    for item in publish["items"]:
        if item["sha256"] in hashes:
            raise ValueError("duplicate image bytes in approved sequence; return to sequence review")
        hashes.add(item["sha256"])
        desired[f"图片/{item['final_filename']}"] = {"sha256": item["sha256"], "source": Path(publish["directory"]) / item["final_filename"]}
    manifest_path = child(folder, MANIFEST)
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if old and (old.get("schema_version") != "1.0" or old.get("product_id") != root.name):
        raise ValueError(f"unrecognized delivery manifest: {manifest_path}")
    managed = old.get("files", {})
    for relative in set(managed) | set(desired):
        target = child(folder, relative)
        if not target.exists():
            continue
        if not target.is_file():
            raise ValueError(f"delivery target is not a file: {target}")
        current = payload.sha256(target)
        if current == desired.get(relative, {}).get("sha256"):
            continue
        if current != managed.get(relative):
            raise ValueError(f"user-modified or unowned file; preserve and resolve conflict: {target}")
    manifest = {"schema_version": "1.0", "product_id": root.name, "artifact_sha256": meta["artifact_sha256"], "files": {k: v["sha256"] for k, v in desired.items()}}
    return {"folder": folder, "desired": desired, "managed": managed, "manifest": manifest}


def apply(plan: dict) -> dict:
    folder, desired = plan["folder"], plan["desired"]
    folder.mkdir(parents=True, exist_ok=True)
    reused = written = 0
    for relative, item in desired.items():
        target = child(folder, relative)
        if target.is_file() and payload.sha256(target) == item["sha256"]:
            reused += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive temporary file avoids overwriting user files on failed runs.
        import tempfile
        fd, name = tempfile.mkstemp(prefix=".交付-", dir=target.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                if "data" in item:
                    stream.write(item["data"])
                else:
                    with item["source"].open("rb") as source:
                        shutil.copyfileobj(source, stream)
            if payload.sha256(tmp) != item["sha256"]:
                raise ValueError(f"source changed during export: {relative}")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        written += 1
    # Delete only prior exporter-owned files with their previously recorded bytes.
    for relative, previous_hash in plan["managed"].items():
        if relative not in desired:
            target = child(folder, relative)
            if target.exists():
                if payload.sha256(target) != previous_hash:
                    raise ValueError(f"changed stale file; preserve: {target}")
                target.unlink()
    for relative, item in desired.items():
        if payload.sha256(child(folder, relative)) != item["sha256"]:
            raise ValueError(f"delivery readback mismatch: {relative}")
    target = child(folder, MANIFEST)
    if not target.exists() or json.loads(target.read_text(encoding="utf-8")) != plan["manifest"]:
        payload.atomic_json(target, plan["manifest"])
    return {"directory": str(folder), "written": written, "reused": reused}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-dir", type=Path, nargs="+", required=True)
    ap.add_argument("--output-root", type=Path, help="Stable delivery root; defaults to workspace/上架交付")
    args = ap.parse_args()
    roots = [p.resolve() for p in args.product_dir]
    if not 1 <= len(roots) <= 3 or len({p.name.casefold() for p in roots}) != len(roots):
        raise ValueError("select 1..3 products with distinct directory names")
    if not args.output_root and len({p.parent.parent for p in roots}) != 1:
        raise ValueError("products from different workspaces need an explicit output root")
    output = (args.output_root or roots[0].parent.parent / "上架交付").resolve()
    plans = [prepare(root, output) for root in roots]
    result = [apply(plan) for plan in plans]
    print(json.dumps({"ok": True, "directory": str(output), "products": result, "archive_created": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
