#!/usr/bin/env python3
"""Build, but never send, a modular Feishu Base batch-update payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from 上架完整性 import CONTRACT, REQUIREMENTS, scope_check


PASS_VALUES = {"pass", "passed", "pass_with_non_blocking_notes"}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value or "")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_hash(root: Path, paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p)):
        h.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(sha256(path).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def qa_passed(qa: dict) -> bool:
    candidates = [qa.get("status")]
    verdict = qa.get("verdict")
    if isinstance(verdict, str):
        candidates.append(verdict)
    elif isinstance(verdict, dict):
        candidates.extend([verdict.get("status"), verdict.get("release_gate"), verdict.get("result")])
    final_gate = qa.get("final_gate")
    if isinstance(final_gate, dict):
        candidates.extend([final_gate.get("status"), final_gate.get("result")])
    statuses = {str(x).lower() for x in candidates if x is not None}
    return bool(statuses & PASS_VALUES) and not bool(statuses & {"fail", "failed", "blocked", "needs_rework"})


def render_detail(detail: dict) -> str:
    parts = []
    internal_keys = {"notes", "consumer_notes", "claim_evidence", "tbc_fields", "tbc_do_not_publish"}
    for key, section in detail.items():
        if key in internal_keys or key.startswith(("_", "internal", "tbc")) or not isinstance(section, dict) or section.get("publish") is False:
            continue
        body = text(section.get("body"))
        confirmed = [f"- {x.get('label') or str(x.get('field', '')).replace('_', ' ').capitalize()}: {text(x.get('value'))}" for x in section.get("confirmed", [])]
        if not body and not confirmed:
            continue
        heading = section.get("heading") or key.replace("_", " ").title()
        parts.append(f"## {heading}\n" + "\n".join(([body] if body else []) + confirmed))
    notes = detail.get("consumer_notes", [])
    if notes:
        parts.append("## Notes\n" + "\n".join(f"- {text(x)}" for x in notes))
    return "\n\n".join(parts)


def render_order(plan: dict, publish_manifest: dict, root: Path) -> str:
    rows = []
    source_items = {item.get("rank"): item for item in plan.get("recommended_order", [])}
    publish_dir = Path(str(publish_manifest["directory"])).resolve()
    relative_dir = publish_dir.relative_to(root).as_posix()
    for item in publish_manifest.get("items", []):
        source = source_items.get(item.get("rank"), {})
        label = item.get("subtitle") or source.get("subtitle") or source.get("role") or "image"
        final_path = f"{relative_dir}/{item.get('final_filename')}"
        row = f"{item.get('rank')}. {final_path} — {label}"
        guard = source.get("release_guardrail") or source.get("guardrail")
        if guard:
            row += f"；{guard}"
        rows.append(row)
    banned = [f"- {x.get('asset')}: {x.get('reason')}" for x in plan.get("do_not_use", [])]
    return "\n".join(rows) + ("\n\n禁用图片：\n" + "\n".join(banned) if banned else "")


def validate_publish_snapshot(root: Path, draft: dict) -> tuple[dict, str]:
    root = root.resolve()
    manifest_path = root / "图片资产清单.json"
    draft_path = root / "文案" / "文案底稿.json"
    translation_path = root / "英文翻译图片" / "图片翻译清单.json"
    audit_path = root / "图片审计" / "GPT图片审计记录.json"
    publish_pointer = root / "最终发布图片" / "当前发布清单.json"
    job_path = root / "产品任务.json"
    required = [job_path, manifest_path, draft_path, audit_path, publish_pointer]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"{root.name}: missing commit artifacts {missing}")
    publish = json.loads(publish_pointer.read_text(encoding="utf-8"))
    publish_dir = Path(str(publish.get("directory", ""))).resolve()
    expected_root = (root / "最终发布图片").resolve()
    if publish_dir != expected_root and expected_root not in publish_dir.parents:
        raise ValueError(f"{root.name}: publish directory escapes product folder")
    sequence = draft.get("image_plan", {}).get("recommended_order", [])
    items = publish.get("items", [])
    if publish.get("count") != len(sequence) or len(items) != len(sequence):
        raise ValueError(f"{root.name}: final publish count differs from sequence")
    for source, item in zip(sequence, items):
        if source.get("rank") != item.get("rank") or source.get("asset") != item.get("source_asset"):
            raise ValueError(f"{root.name}: final publish manifest no longer matches rank {source.get('rank')}")
        target = (publish_dir / str(item.get("final_filename", ""))).resolve()
        original = (root / str(source.get("asset", ""))).resolve()
        if target.parent != publish_dir or root not in original.parents or not original.is_file():
            raise ValueError(f"{root.name}: invalid final/source image path")
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError(f"{root.name}: missing final publish image {target.name}")
        if not item.get("sha256") or sha256(target) != str(item["sha256"]).lower() or sha256(original) != str(item["sha256"]).lower():
            raise ValueError(f"{root.name}: changed final publish image {target.name}")
    hash_paths = [job_path, manifest_path, draft_path, audit_path, publish_pointer]
    if (root / REQUIREMENTS).exists():
        hash_paths.append(root / REQUIREMENTS)
    issue_path = root / "质量检查" / "问题处理记录.json"
    if issue_path.exists():
        hash_paths.append(issue_path)
    if translation_path.exists():
        hash_paths.append(translation_path)
    return publish, artifact_hash(root, hash_paths)


def build_values(root: Path, require_qa: bool, *, allow_committed: bool = False) -> tuple[dict, dict]:
    root = root.resolve()
    draft = json.loads((root / "文案" / "文案底稿.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "图片资产清单.json").read_text(encoding="utf-8"))
    qa_path = root / "质量检查" / "质量检查报告.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8")) if qa_path.exists() else {}
    script_qa_path = root / "质量检查" / "自动校验报告.json"
    script_qa = json.loads(script_qa_path.read_text(encoding="utf-8")) if script_qa_path.exists() else {}
    publish, current_hash = validate_publish_snapshot(root, draft)
    if require_qa and not qa_passed(qa):
        raise ValueError(f"{root.name}: QA is not PASS")
    if require_qa and qa.get("draft_sha256") != sha256(root / "文案" / "文案底稿.json"):
        raise ValueError(f"{root.name}: copy QA is missing or stale for the current draft")
    if require_qa and not qa_passed(script_qa):
        raise ValueError(f"{root.name}: script validation is not PASS")
    job = json.loads((root / "产品任务.json").read_text(encoding="utf-8"))
    if require_qa:
        if script_qa.get("completion_contract") != CONTRACT:
            raise ValueError(f"{root.name}: old QA lacks parent scope and final-output information coverage; run current QA")
        scope_check(root, job)
    gallery_mode = job.get("asset_collection_mode", "gallery_all") == "gallery_all"
    if require_qa and gallery_mode and script_qa.get("audit_scope") != "gallery_all_screened_listing_selected_detailed":
        raise ValueError(f"{root.name}: old selected-only QA cannot prove full gallery screening; run current QA")
    state_path = root / "执行状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    allowed_states = {"qa_passed", "ready_to_commit"} | ({"committed"} if allow_committed else set())
    if require_qa and state.get("status") not in allowed_states:
        raise ValueError(f"{root.name}: state is not qa_passed")
    if require_qa and any(doc.get("artifact_sha256") != current_hash for doc in (script_qa, state)):
        raise ValueError(f"{root.name}: validated artifact snapshot has changed; run QA again")
    copy = draft.get("copy", {})
    bullets = "\n".join(f"- {text(x)}" for x in copy.get("english_bullets", []))
    items = manifest.get("items", [])
    positions = len(items) or int(manifest.get("source_position_count") or 0)
    strict = len({x.get("sha256") for x in items if x.get("download_status") == "success" and x.get("sha256")})
    selected_count = len(publish.get("items", []))
    translation_path = root / "英文翻译图片" / "图片翻译清单.json"
    translations = json.loads(translation_path.read_text(encoding="utf-8")) if translation_path.exists() else {}
    audit_path = root / "图片审计" / "GPT图片审计记录.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    chat_url = audit.get("chatgpt_conversation_url") or translations.get("chatgpt_conversation_url") or (json.loads((root / "执行状态.json").read_text(encoding="utf-8")).get("chatgpt_conversation_url") if (root / "执行状态.json").exists() else None)
    qa_label = "最终QA：PASS（尚未写入飞书）" if require_qa else "预览：未验证QA，禁止提交"
    selected_translation_count = sum(bool(x.get("trace_source_asset")) for x in publish.get("items", []))
    status = f"{qa_label}；采用图片 {selected_count} 张，其中译图 {selected_translation_count} 张；检查范围为采用图片及其原图，不代表页面全量审阅。已发现位置 {positions}；已下载唯一文件 {strict}。素材包：{root}"
    if gallery_mode:
        gallery = script_qa.get("gallery_summary") or {}
        status = f"{qa_label}；主图区 {gallery.get('expected_positions')} 个位置，下载 {gallery.get('downloaded_positions')}，去重后初筛 {gallery.get('screened_unique_images')} 张；采用主图区来源 {gallery.get('adopted_gallery_sources')} 张、其他来源 {gallery.get('adopted_other_sources')} 张；最终 {selected_count} 张，其中译图 {selected_translation_count} 张。采用图已详细复核，未抓取网站全部图片。素材目录：{root}"
    values = {
        "中文标题": text(copy.get("chinese_title")),
        "英文标题": text(copy.get("tiktok_shop_english_title") or copy.get("english_title")),
        "英文卖点": bullets,
        "图片排列顺序": render_order(draft.get("image_plan", {}), publish, root),
        "完整详情页": render_detail(copy.get("detail_page", {})),
        "素材与处理状态": status,
    }
    if chat_url:
        values["GPT处理对话"] = f"[{root.name} 图片处理对话]({chat_url})"
    empty = [k for k, v in values.items() if not str(v).strip()]
    if empty:
        raise ValueError(f"{root.name}: empty required fields {empty}")
    return values, {"product_id": root.name, "artifact_sha256": current_hash, "state_path": str(state_path)}


def mark_ready(root: Path, record_id: str, artifact_sha256: str, commit_token: str) -> None:
    state_path = root / "执行状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema_version": "1.0", "product_id": root.name}
    state.update({
        "status": "ready_to_commit",
        "artifact_sha256": artifact_sha256,
        "commit_token": commit_token,
        "target_record_id": record_id,
        "last_error": None,
        "next_action": "single writer may commit payload, then read back",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    state["completed_stages"] = list(dict.fromkeys([*state.get("completed_stages", []), "qa_passed", "payload_prepared"]))
    atomic_json(state_path, state)
    round_id = state.get("round_id")
    round_path = root.parent.parent / "产品批次" / f"{round_id}.json" if round_id else None
    if round_path and round_path.exists():
        round_doc = json.loads(round_path.read_text(encoding="utf-8"))
        for product in round_doc.get("products", []):
            if str(product.get("product_id")) == root.name:
                product.update({"status": "ready_to_commit", "phase": "commit", "artifact_sha256": artifact_sha256, "commit_token": commit_token})
        round_doc["updated_at"] = state["updated_at"]
        atomic_json(round_path, round_doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--records", required=True, type=Path, help="JSON object mapping product_id to record_id")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--metadata-output", type=Path)
    ap.add_argument("--allow-without-qa", action="store_true")
    args = ap.parse_args()
    mapping = json.loads(args.records.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 3:
        raise SystemExit("records must map 1..3 product IDs to record IDs")
    updates = {}
    prepared = []
    if len({str(x) for x in mapping.values()}) != len(mapping):
        raise SystemExit("multiple products cannot target the same record ID")
    for product_id, record_id in mapping.items():
        if not str(record_id).startswith("rec"):
            raise SystemExit(f"{product_id}: invalid record ID")
        root = args.workspace.resolve() / "产品工作区" / str(product_id)
        values, meta = build_values(root, not args.allow_without_qa)
        record_id = str(record_id)
        token_input = f"{product_id}|{record_id}|{meta['artifact_sha256']}"
        commit_token = hashlib.sha256(token_input.encode("utf-8")).hexdigest()
        updates[record_id] = values
        prepared.append({**meta, "record_id": record_id, "commit_token": commit_token})
    payload = {"preview_records" if args.allow_without_qa else "update_records": updates}
    atomic_json(args.output, payload)
    metadata_output = args.metadata_output or args.output.with_name(f"{args.output.stem}_提交信息.json")
    atomic_json(metadata_output, {"schema_version": "1.1", "sent": False, "committable": not args.allow_without_qa, "products": prepared})
    if not args.allow_without_qa:
        for item in prepared:
            root = args.workspace.resolve() / "产品工作区" / item["product_id"]
            mark_ready(root, item["record_id"], item["artifact_sha256"], item["commit_token"])
    print(json.dumps({"ok": True, "record_count": len(updates), "output": str(args.output), "metadata_output": str(metadata_output), "sent": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
