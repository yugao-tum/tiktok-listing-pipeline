#!/usr/bin/env python3
"""Create five-image translation batches and degrade failed groups to 3/2 then 1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


TRANSLATE_STATES = {"yes_german", "yes_non_english"}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def candidate_items(raw: object, source_language: str, target_language: str, prompt_version: str) -> list[dict]:
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("input must be an array or object with items[]")
    result = []
    seen = set()
    for item in items:
        required = item.get("translation_required") is True or item.get("text_language_judgment") in TRANSLATE_STATES
        if not required or item.get("is_duplicate") is True or item.get("publish_decision") in {"exclude", "uncertain_requires_review"}:
            continue
        filename = item.get("filename") or item.get("asset")
        sha = item.get("sha256")
        if not filename or not sha:
            raise ValueError("translation candidate missing filename/asset or sha256")
        key = sha.lower()
        if key in seen:
            continue
        seen.add(key)
        idem = hashlib.sha256(f"{key}|{source_language}|{target_language}|{prompt_version}".encode()).hexdigest()
        result.append({"asset": filename, "sha256": key, "idempotency_key": idem, "status": "pending"})
    return result


def create(args: argparse.Namespace) -> int:
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    items = candidate_items(raw, args.source_language, args.target_language, args.prompt_version)
    batches = []
    for index in range(0, len(items), args.batch_size):
        group = items[index:index + args.batch_size]
        batches.append({"batch_id": f"b{len(batches)+1:03d}", "level": len(group), "status": "pending", "parent_batch_id": None, "items": group, "attempts": []})
    plan = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_language": args.source_language,
        "target_language": args.target_language,
        "prompt_version": args.prompt_version,
        "fallback": [args.batch_size, 3, 1],
        "candidate_count": len(items),
        "batches": batches,
    }
    atomic_json(args.output, plan)
    print(json.dumps({"ok": True, "candidates": len(items), "batches": len(batches), "output": str(args.output)}, ensure_ascii=False))
    return 0


def degrade(args: argparse.Namespace) -> int:
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    parent = next((b for b in plan.get("batches", []) if b.get("batch_id") == args.batch_id), None)
    if not parent:
        raise SystemExit(f"batch not found: {args.batch_id}")
    if parent.get("status") not in {"pending", "failed", "running"}:
        raise SystemExit(f"batch cannot be degraded from status {parent.get('status')}")
    items = [x for x in parent.get("items", []) if x.get("status") != "success"]
    if not items:
        parent["status"] = "success"
        atomic_json(args.output or args.plan, plan)
        print(json.dumps({"ok": True, "batch_id": args.batch_id, "new_batches": [], "failed_items": 0}))
        return 0
    terminal = len(items) == 1 and int(parent.get("level", len(parent["items"]))) == 1
    parent["status"] = "terminal_failed" if terminal else "degraded"
    parent.setdefault("attempts", []).append({"at": datetime.now(timezone.utc).isoformat(), "result": "failed", "reason": args.reason})
    if terminal:
        atomic_json(args.output or args.plan, plan)
        print(json.dumps({"ok": False, "terminal": True, "batch_id": args.batch_id, "failed_items": len(items)}, ensure_ascii=False))
        return 9
    size = 3 if len(items) > 3 else 1
    next_number = len(plan["batches"]) + 1
    children = []
    for pos in range(0, len(items), size):
        group = items[pos:pos + size]
        child = {"batch_id": f"b{next_number:03d}", "level": len(group), "status": "pending", "parent_batch_id": args.batch_id, "items": group, "attempts": []}
        next_number += 1
        children.append(child)
    plan["batches"].extend(children)
    atomic_json(args.output or args.plan, plan)
    print(json.dumps({"ok": True, "parent": args.batch_id, "new_batches": [b["batch_id"] for b in children], "sizes": [len(b["items"]) for b in children]}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--input", required=True, type=Path)
    c.add_argument("--output", required=True, type=Path)
    c.add_argument("--source-language", default="de")
    c.add_argument("--target-language", default="en")
    c.add_argument("--prompt-version", default="v1")
    c.add_argument("--batch-size", type=int, default=5, choices=range(1, 6))
    c.set_defaults(func=create)
    d = sub.add_parser("degrade")
    d.add_argument("--plan", required=True, type=Path)
    d.add_argument("--batch-id", required=True)
    d.add_argument("--reason", required=True)
    d.add_argument("--output", type=Path)
    d.set_defaults(func=degrade)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
