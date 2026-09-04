#!/usr/bin/env python3
"""Initialize one recoverable round containing at most three product jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_products(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    products = raw.get("products") if isinstance(raw, dict) else raw
    if not isinstance(products, list) or not products:
        raise ValueError("input must be a non-empty list or an object with products[]")
    return products


def validate_product(product: dict) -> None:
    pid = str(product.get("product_id", "")).strip()
    url = str(product.get("product_url", "")).strip()
    if not pid or any(c in pid for c in '<>:"/\\|?*'):
        raise ValueError(f"invalid product_id: {pid!r}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"product {pid}: product_url must be HTTPS")
    variant = product.get("variant") or {}
    if variant and (not str(variant.get("id", "")).strip() or not str(variant.get("sku", "")).strip()):
        raise ValueError(f"product {pid}: variant must contain both id and sku when supplied")
    excluded = product.get("excluded_variants") or []
    target_id = str(variant.get("id", ""))
    target_sku = str(variant.get("sku", ""))
    for item in excluded:
        if str(item.get("id", "")) == target_id and target_id:
            raise ValueError(f"product {pid}: target variant also appears in excluded_variants")
        if str(item.get("sku", "")) == target_sku and target_sku:
            raise ValueError(f"product {pid}: target SKU also appears in excluded_variants")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True, type=Path, help="JSON list or {products:[...]}")
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--round-id", default=None)
    ap.add_argument("--max-products", type=int, default=3)
    args = ap.parse_args()

    workspace = args.workspace.resolve()
    if workspace == Path(workspace.anchor):
        raise SystemExit("refusing to use a drive root as workspace")
    products = load_products(args.jobs)
    if not 1 <= len(products) <= args.max_products:
        raise SystemExit(f"round must contain 1..{args.max_products} products")
    ids = [str(p.get("product_id", "")).strip() for p in products]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate product_id in round")
    for product in products:
        validate_product(product)

    round_id = args.round_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now = datetime.now(timezone.utc).isoformat()
    leases = []
    for product in products:
        pid = str(product["product_id"]).strip()
        root = workspace / "产品工作区" / pid
        for name in ("原始图片", "英文翻译图片", "最终发布图片", "图片审计", "文案", "质量检查"):
            (root / name).mkdir(parents=True, exist_ok=True)
        job = {"schema_version": "1.1", **product}
        job["image_analysis_mode"] = "gpt_in_app_browser_chatgpt"
        job_hash = digest(job)
        job_path = root / "产品任务.json"
        state_path = root / "执行状态.json"
        if job_path.exists():
            existing = json.loads(job_path.read_text(encoding="utf-8"))
            if digest(existing) != job_hash:
                raise SystemExit(f"product {pid}: existing 产品任务.json differs; resolve it explicitly")
        else:
            atomic_json(job_path, job)
        identity_complete = bool((job.get("variant") or {}).get("id") and (job.get("variant") or {}).get("sku"))
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("job_sha256") != job_hash:
                raise SystemExit(f"product {pid}: 执行状态.json belongs to a different job")
        else:
            state = {
                "schema_version": "1.0",
                "product_id": pid,
                "round_id": round_id,
                "status": "queued",
                "identity_complete": identity_complete,
                "job_sha256": job_hash,
                "completed_stages": [],
                "last_error": None,
                "next_action": "confirm exact variant" if not identity_complete else "collect evidence",
                "created_at": now,
                "updated_at": now,
            }
            atomic_json(state_path, state)
            with (root / "执行事件.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(canonical_json({"at": now, "event": "job_initialized", "round_id": round_id, "job_sha256": job_hash}) + "\n")
        leases.append({"product_id": pid, "status": state["status"], "identity_complete": identity_complete, "image_analysis_mode": job["image_analysis_mode"], "owner": None, "phase": "intake"})

    round_doc = {"schema_version": "1.0", "round_id": round_id, "max_products": args.max_products, "created_at": now, "products": leases}
    round_path = workspace / "产品批次" / f"{round_id}.json"
    if round_path.exists():
        existing = json.loads(round_path.read_text(encoding="utf-8"))
        if {x["product_id"] for x in existing.get("products", [])} != set(ids):
            raise SystemExit(f"round {round_id} already exists with different products")
    else:
        atomic_json(round_path, round_doc)
    print(json.dumps({"ok": True, "round_id": round_id, "product_count": len(products), "round_file": str(round_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
