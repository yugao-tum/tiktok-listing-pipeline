#!/usr/bin/env python3
"""Read-only family reconciliation against current QA and delivered files."""
import argparse
import json
from pathlib import Path

from 上架完整性 import load_scope, read, sha
from 汇总上架交付文件 import prepare, child


def reconcile(workspace, reference, output_root=None):
    workspace = workspace.resolve()
    scope = load_scope(workspace, reference)
    rows = []
    for variant in scope["variants"]:
        if variant["disposition"] == "excluded":
            continue
        pid = variant["product_id"]
        row = {"product_id": pid, "variant_id": variant["variant_id"], "sku": variant["sku"], "options": variant["options"]}
        root = workspace / "产品工作区" / pid
        if variant["disposition"] == "deferred":
            row.update(status="deferred", reason=variant["reason"])
        elif not (root / "产品任务.json").exists():
            row.update(status="not_queued", reason="required variant has no job")
        else:
            try:
                job = read(root / "产品任务.json")
                if job.get("scope") != reference or str(job.get("variant", {}).get("id")) != str(variant["variant_id"]) or job.get("variant", {}).get("sku") != variant["sku"]:
                    raise ValueError("job differs from family scope")
                plan = prepare(root, (output_root or workspace / "上架交付").resolve())
                manifest = plan["folder"] / "交付清单.json"
                if not manifest.is_file() or read(manifest) != plan["manifest"]:
                    raise ValueError("delivery manifest missing or stale")
                for name, item in plan["desired"].items():
                    target = child(plan["folder"], name)
                    if not target.is_file() or sha(target) != item["sha256"]:
                        raise ValueError(f"delivery file missing or changed: {name}")
                row.update(status="delivered", image_counts=plan["manifest"]["image_counts"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                row.update(status="incomplete", reason=str(exc))
        rows.append(row)
    delivered = sum(x["status"] == "delivered" for x in rows)
    return {"family_id": scope["family_id"], "scope_sha256": reference["sha256"], "status": "complete" if delivered == len(rows) else "incomplete", "expected_variants": len(rows), "delivered_variants": delivered, "variants": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scope-file", required=True, help="workspace-relative JSON file")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    reference = {"file": args.scope_file, "sha256": sha(child(workspace, args.scope_file))}
    report = reconcile(workspace, reference, args.output_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 12


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
