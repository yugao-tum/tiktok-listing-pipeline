"""Mechanical scope and final-output evidence checks; no visual judgments."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT = "1.6"
REQUIREMENTS = "图片审计/购买信息需求.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def child(root, name):
    path = (root / str(name)).resolve()
    if not name or root.resolve() not in path.parents:
        raise ValueError("scope path must stay within workspace")
    return path


def load_scope(workspace, reference):
    path = child(workspace, reference.get("file"))
    if sha(path) != reference.get("sha256"):
        raise ValueError("product scope missing or changed; reconcile scope before QA")
    doc = read(path)
    if not doc.get("family_id") or not doc.get("scope_evidence") or doc.get("requested_scope") not in {"all_model_variants", "exact_variants"}:
        raise ValueError("scope needs family identity, requested scope and user evidence")
    source = doc.get("snapshot", {})
    snapshot = child(workspace, source.get("file"))
    if sha(snapshot) != source.get("sha256"):
        raise ValueError("variant inventory snapshot changed")
    raw = read(snapshot)
    inventory = raw.get("product", raw).get("variants", [])
    rows = doc.get("variants", [])
    original = {str(x["id"]): x for x in inventory}
    ids = [str(x.get("variant_id", "")) for x in rows]
    if not original or len(original) != len(inventory) or len(ids) != len(set(ids)) or set(ids) != set(original):
        raise ValueError("scope must classify every snapshot variant exactly once")
    jobs = []
    for row in rows:
        source = original[str(row["variant_id"])]
        if row.get("sku") != source.get("sku") or row.get("options") != source.get("options"):
            raise ValueError("scope SKU/options differ from official snapshot")
        decision = row.get("disposition")
        if decision in {"required", "deferred"}:
            pid = str(row.get("product_id", ""))
            if not pid or pid in {".", ".."} or any(c in pid for c in '<>:"/\\|?*'):
                raise ValueError("each in-scope variant needs a safe product_id")
            jobs.append(pid.casefold())
            if decision == "deferred" and not row.get("reason"):
                raise ValueError("deferred variant needs a reason; it remains incomplete")
        elif decision == "excluded":
            if row.get("reason_code") not in {"other_model", "user_excluded"} or not row.get("reason") or not row.get("evidence"):
                raise ValueError("scope exclusion needs model or user evidence; unavailable is not an exclusion")
        else:
            raise ValueError("unresolved variant scope")
    if not jobs or len(jobs) != len(set(jobs)):
        raise ValueError("scope needs distinct in-scope product jobs")
    return doc


def scope_check(root, job):
    doc = load_scope(root.parent.parent, job.get("scope", {}))
    variant = job.get("variant", {})
    matches = [x for x in doc["variants"] if x.get("product_id") == root.name and x.get("disposition") == "required"]
    if len(matches) != 1 or str(matches[0]["variant_id"]) != str(variant.get("id")) or matches[0]["sku"] != variant.get("sku"):
        raise ValueError("job is not the required variant in its parent scope")
    return {"family_id": doc["family_id"], "required": sum(x["disposition"] == "required" for x in doc["variants"]), "deferred": sum(x["disposition"] == "deferred" for x in doc["variants"]), "scope_sha256": job["scope"]["sha256"], "completion_level": "variant_only"}


def information_check(root, draft, audit):
    path = root / REQUIREMENTS
    if not path.is_file() or audit.get("requirements_sha256") != sha(path):
        raise ValueError("purchase-information baseline missing or not reviewed")
    rows = read(path).get("requirements", [])
    ids = [x.get("id") for x in rows]
    if not rows or any(not isinstance(x, str) or not x.strip() for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("purchase-information requirements need unique IDs")
    sequence = {x.get("rank"): x for x in draft.get("image_plan", {}).get("recommended_order", [])}
    review = audit.get("final_sequence_review", {})
    evidence = review.get("requirement_coverage", {})
    if review.get("information_completeness") != "PASS":
        raise ValueError("GPT must review complete purchasing information, not image count")
    required = 0
    for row in rows:
        if not row.get("title") or not row.get("source_evidence") or type(row.get("required")) is not bool:
            raise ValueError("requirement needs title, official evidence and required flag")
        if not row["required"]:
            if not row.get("omission_reason"):
                raise ValueError("optional information needs a specific omission reason")
            continue
        required += 1
        supports = evidence.get(row["id"], [])
        if not isinstance(supports, list) or not supports:
            raise ValueError(f"required information uncovered: {row['id']}")
        for support in supports:
            item = sequence.get(support.get("rank"), {})
            output = child(root, item.get("asset"))
            if not output.is_file() or support.get("output_sha256") != sha(output):
                raise ValueError(f"coverage must bind actual final output: {row['id']}")
            if support.get("status") != "PASS" or not support.get("visible_region") or not support.get("shows"):
                raise ValueError(f"coverage needs readable region and specific visible information: {row['id']}")
    if not required:
        raise ValueError("at least one required purchase-information item is needed")
    for item in audit.get("gallery_screening", []):
        links = item.get("requirement_ids")
        if not isinstance(links, list) or any(x not in ids for x in links):
            raise ValueError("gallery screening must map information to known requirements, including excluded images")
    return {"required_information": required, "covered_information": required}
