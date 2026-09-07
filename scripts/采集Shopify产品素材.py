#!/usr/bin/env python3
"""采集 Shopify 商品证据与图片，生成 TikTok Listing Pipeline 兼容素材清单。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Safari/537.36"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def safe_filename(value: str) -> str:
    value = INVALID_FILENAME.sub("_", value.strip())
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value or "图片"


def canonical_url(value: str, base_url: str) -> str | None:
    value = (value or "").strip()
    if not value or value.startswith(("data:", "blob:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base_url, value)
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    suffix = Path(parts.path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return None
    # Shopify resizing parameters do not identify a different source asset.
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k == "v"]
    return urlunsplit(("https", parts.netloc.lower(), parts.path, urlencode(keep), ""))


def best_img_url(tag, base_url: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for attr in ("srcset", "data-srcset"):
        raw = tag.get(attr)
        if not raw:
            continue
        for part in raw.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = 0
            if len(bits) > 1 and bits[-1].endswith("w"):
                try:
                    width = int(bits[-1][:-1])
                except ValueError:
                    width = 0
            candidates.append((width, bits[0]))
    for attr in ("data-zoom-image", "data-master", "data-src", "src"):
        raw = tag.get(attr)
        if raw:
            candidates.append((0, raw))
    for _, raw in sorted(candidates, reverse=True):
        normalized = canonical_url(raw, base_url)
        if normalized:
            return normalized
    return None


def get_json(session: requests.Session, url: str) -> tuple[object, bytes]:
    response = session.get(url, timeout=45)
    response.raise_for_status()
    raw = response.content
    return response.json(), raw


def pick_product(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Shopify product response is not an object")
    product = raw.get("product") if isinstance(raw.get("product"), dict) else raw
    if not isinstance(product, dict) or not product.get("id"):
        raise ValueError("Shopify product object is missing id")
    return product


def append_evidence(path: Path, event: dict) -> None:
    event_core = {k: v for k, v in event.items() if k not in {"at", "evidence_id"}}
    evidence_id = hashlib.sha256(
        json.dumps(event_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    existing = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                existing.add(json.loads(line).get("evidence_id"))
            except Exception:
                continue
    if evidence_id in existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": now_iso(), "evidence_id": evidence_id, **event_core}
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def variant_scope(variant_ids: list, target_id: str, excluded_ids: set[str]) -> str:
    ids = {str(x) for x in variant_ids or []}
    if target_id in ids:
        return "exact_variant"
    if ids & excluded_ids:
        return "other_variant"
    if ids:
        return "other_variant"
    return "shared_unassigned"


def collect_positions(product_json: dict, product_js: dict, page_html: str, base_url: str) -> list[dict]:
    positions: list[dict] = []
    image_records = product_json.get("images") or product_js.get("images") or []
    for index, image in enumerate(image_records, start=1):
        if isinstance(image, str):
            raw_url, variant_ids, original_position = image, [], index
        else:
            raw_url = image.get("src") or image.get("url") or ""
            variant_ids = image.get("variant_ids") or []
            original_position = image.get("position") or index
        url = canonical_url(raw_url, base_url)
        if url:
            positions.append(
                {
                    "section": "gallery",
                    "section_position": int(original_position),
                    "url": raw_url,
                    "canonical_url": url,
                    "variant_ids": [str(x) for x in variant_ids],
                }
            )

    body_html = str(product_json.get("body_html") or product_json.get("description") or product_js.get("description") or "")
    body_soup = BeautifulSoup(body_html, "html.parser")
    for index, tag in enumerate(body_soup.find_all("img"), start=1):
        url = best_img_url(tag, base_url)
        if url:
            positions.append(
                {
                    "section": "description",
                    "section_position": index,
                    "url": tag.get("src") or tag.get("data-src") or url,
                    "canonical_url": url,
                    "variant_ids": [],
                }
            )

    # Product specifications are often rendered outside body_html in a collapsible block.
    page_soup = BeautifulSoup(page_html, "html.parser")
    spec_tags = []
    for container in page_soup.find_all(["details", "section", "div"]):
        marker = " ".join(container.get("class", [])) + " " + str(container.get("id") or "")
        text = container.get_text(" ", strip=True)[:500]
        if re.search(r"specif|spezifikation", marker + " " + text, re.I):
            direct = container.find_all("img")
            if direct and len(direct) <= 8:
                spec_tags = direct
                break
    body_urls = [x["canonical_url"] for x in positions if x["section"] == "description"]
    spec_index = 0
    for tag in spec_tags:
        url = best_img_url(tag, base_url)
        if not url or url in body_urls:
            continue
        spec_index += 1
        positions.append(
            {
                "section": "specification",
                "section_position": spec_index,
                "url": tag.get("src") or tag.get("data-src") or url,
                "canonical_url": url,
                "variant_ids": [],
            }
        )
    for index, item in enumerate(positions, start=1):
        item["source_position"] = index
    return positions


def image_extension(content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }
    return by_type.get(content_type) or Path(urlsplit(url).path).suffix.lower() or ".jpg"


def build_contact_sheets(product_root: Path, unique_items: list[dict]) -> list[str]:
    output_dir = product_root / "质量检查"
    output_dir.mkdir(parents=True, exist_ok=True)
    sheet_paths: list[str] = []
    per_sheet, thumb_w, thumb_h, label_h = 6, 500, 420, 70
    font = ImageFont.load_default()
    for page_index in range(0, len(unique_items), per_sheet):
        page_items = unique_items[page_index : page_index + per_sheet]
        canvas = Image.new("RGB", (thumb_w * 3, (thumb_h + label_h) * 2), "white")
        draw = ImageDraw.Draw(canvas)
        for slot, item in enumerate(page_items):
            row, col = divmod(slot, 3)
            x, y = col * thumb_w, row * (thumb_h + label_h)
            source = product_root / "原始图片" / item["filename"]
            with Image.open(source) as im:
                im = im.convert("RGB")
                im.thumbnail((thumb_w - 20, thumb_h - 20))
                px = x + (thumb_w - im.width) // 2
                py = y + (thumb_h - im.height) // 2
                canvas.paste(im, (px, py))
            label = f'{item["source_position"]:02d} {item["section"]}:{item["section_position"]} {item["variant_scope"]}\n{item["filename"]}'
            draw.multiline_text((x + 8, y + thumb_h + 5), label, fill="black", font=font, spacing=3)
        number = page_index // per_sheet + 1
        path = output_dir / f"资产总览_{number:02d}.jpg"
        canvas.save(path, format="JPEG", quality=88)
        sheet_paths.append(str(path.relative_to(product_root)).replace("\\", "/"))
    return sheet_paths


def update_state(product_root: Path, status: str, stages: list[str], next_action: str) -> None:
    state_path = product_root / "执行状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = status
    state["completed_stages"] = list(dict.fromkeys([*state.get("completed_stages", []), *stages]))
    state["last_error"] = None
    state["next_action"] = next_action
    state["updated_at"] = now_iso()
    atomic_json(state_path, state)
    round_id = state.get("round_id")
    round_path = product_root.parent.parent / "产品批次" / f"{round_id}.json" if round_id else None
    if round_path and round_path.exists():
        round_doc = json.loads(round_path.read_text(encoding="utf-8"))
        for product in round_doc.get("products", []):
            if str(product.get("product_id")) == product_root.name:
                product["status"] = status
                product["phase"] = "assets"
        round_doc["updated_at"] = state["updated_at"]
        atomic_json(round_path, round_doc)


def collect(product_root: Path, selected_positions: list[int] | None = None, all_images: bool = False, discovery_only: bool = False) -> dict:
    if sum((selected_positions is not None, all_images, discovery_only)) > 1:
        raise ValueError("collection modes are mutually exclusive")
    job = json.loads((product_root / "产品任务.json").read_text(encoding="utf-8"))
    base_url = str(job["product_url"]).rstrip("/")
    target_id = str(job["variant"]["id"])
    target_sku = str(job["variant"]["sku"])
    excluded_ids = {str(x.get("id")) for x in job.get("excluded_variants", [])}

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
    page_response = session.get(base_url, timeout=45)
    page_response.raise_for_status()
    page_raw = page_response.content
    page_html = page_response.text
    json_raw_obj, json_raw = get_json(session, base_url + ".json")
    js_raw_obj, js_raw = get_json(session, base_url + ".js")
    product_json = pick_product(json_raw_obj)
    product_js = pick_product(js_raw_obj)

    variants = product_js.get("variants") or product_json.get("variants") or []
    matches = [x for x in variants if str(x.get("id")) == target_id and str(x.get("sku")) == target_sku]
    if len(matches) != 1:
        raise RuntimeError(f"target variant mismatch: id={target_id}, sku={target_sku}, matches={len(matches)}")

    snapshot_dir = product_root / "证据快照"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    page_hash = sha256_bytes(page_raw)
    json_hash = sha256_bytes(json_raw)
    js_hash = sha256_bytes(js_raw)
    page_path = snapshot_dir / f"商品页_{page_hash[:12]}.html"
    json_path = snapshot_dir / f"商品数据JSON_{json_hash[:12]}.json"
    js_path = snapshot_dir / f"商品数据JS_{js_hash[:12]}.json"
    if not page_path.exists():
        page_path.write_bytes(page_raw)
    if not json_path.exists():
        json_path.write_bytes(json_raw)
    if not js_path.exists():
        js_path.write_bytes(js_raw)

    positions = collect_positions(product_json, product_js, page_html, base_url)
    if not positions:
        raise RuntimeError("no product images discovered")

    # Discovery records URLs only; it must not replace an existing listing manifest.
    discovery = [{**x, "variant_scope": variant_scope(x.get("variant_ids") or [], target_id, excluded_ids)} for x in positions]
    atomic_json(product_root / "图片发现清单.json", {"scope": "discovered_only", "items": discovery})
    if discovery_only:
        return {"product_id": product_root.name, "mode": "discovery_only", "positions": len(positions), "image_downloads": 0, "next_action": "select candidate positions with --positions"}
    gallery_only = selected_positions is None and not all_images
    gallery_expected = len(product_json.get("images") or product_js.get("images") or [])
    gallery_positions = [x for x in positions if x["section"] == "gallery"]
    if gallery_only and (not gallery_positions or len(gallery_positions) != gallery_expected):
        raise ValueError("gallery inventory missing or incomplete; verify product gallery instead of collecting page images")
    chosen = set(selected_positions or [])
    unknown = chosen - {x["source_position"] for x in positions}
    if unknown:
        raise ValueError(f"unknown candidate positions: {sorted(unknown)}")
    selected_urls = {x["canonical_url"] for x in positions if all_images or x["source_position"] in chosen or (gallery_only and x["section"] == "gallery")}
    manifest_path = product_root / "图片资产清单.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if previous and (str(previous.get("variant_id")) != target_id or str(previous.get("sku")) != target_sku):
        raise ValueError("existing manifest belongs to a different variant")
    previous_by_url = {x.get("canonical_url"): x for x in previous.get("items", []) if x.get("download_status") == "success"}

    source_dir = product_root / "原始图片"
    source_dir.mkdir(parents=True, exist_ok=True)
    url_first: dict[str, dict] = {}
    hash_first: dict[str, dict] = {}
    failed: list[dict] = []
    manifest_items: list[dict] = []

    for position in positions:
        url = position["canonical_url"]
        scope = variant_scope(position.get("variant_ids") or [], target_id, excluded_ids)
        item = {
            **position,
            "original_name": Path(urlsplit(url).path).name,
            "variant_scope": scope,
            "duplicate_of": None,
            "is_duplicate": False,
            "filename": None,
            "sha256": None,
            "width": None,
            "height": None,
            "mime": None,
            "text_language_judgment": "uncertain_requires_review",
            "download_status": "pending",
            "visual_duplicate_of": None,
        }
        prior = previous_by_url.get(url, {})
        prior_file = source_dir / str(prior.get("filename", ""))
        if prior.get("sha256") and prior_file.is_file() and sha256_file(prior_file) == prior["sha256"]:
            item.update({key: prior.get(key) for key in ("filename", "sha256", "width", "height", "mime", "download_status")})
            url_first[url] = item
            hash_first.setdefault(item["sha256"], item)
            manifest_items.append(item)
            continue
        if url not in selected_urls:
            item["download_status"] = "not_selected"
            manifest_items.append(item)
            continue
        if url in url_first:
            first = url_first[url]
            item.update(
                {
                    "duplicate_of": first["source_position"],
                    "is_duplicate": True,
                    "filename": first["filename"],
                    "sha256": first["sha256"],
                    "width": first["width"],
                    "height": first["height"],
                    "mime": first["mime"],
                    "download_status": first["download_status"],
                }
            )
            manifest_items.append(item)
            continue
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            content = response.content
            image_hash = sha256_bytes(content)
            with Image.open(BytesIO(content)) as image:
                image.verify()
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                detected_format = (image.format or "").upper()
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if not mime.startswith("image/"):
                mime = Image.MIME.get(detected_format, "")
            extension = image_extension(mime, url)
            if image_hash in hash_first:
                first = hash_first[image_hash]
                item.update(
                    {
                        "duplicate_of": first["source_position"],
                        "is_duplicate": True,
                        "filename": first["filename"],
                        "sha256": image_hash,
                        "width": width,
                        "height": height,
                        "mime": mime,
                        "download_status": "success",
                    }
                )
            else:
                stem = safe_filename(Path(urlsplit(url).path).stem)[:55]
                filename = f'{position["source_position"]:02d}_{position["section"]}_{stem}_{image_hash[:8]}{extension}'
                target = source_dir / filename
                if target.exists() and sha256_file(target) != image_hash:
                    raise RuntimeError(f"existing source filename has different bytes: {filename}")
                if not target.exists():
                    part = target.with_suffix(target.suffix + ".part")
                    part.write_bytes(content)
                    os.replace(part, target)
                item.update(
                    {
                        "filename": filename,
                        "sha256": image_hash,
                        "width": width,
                        "height": height,
                        "mime": mime,
                        "download_status": "success",
                    }
                )
                hash_first[image_hash] = item
            url_first[url] = item
        except Exception as exc:
            item["download_status"] = "failed"
            item["error"] = str(exc)
            failed.append({"source_position": item["source_position"], "url": url, "error": str(exc)})
            url_first[url] = item
        manifest_items.append(item)

    successful_unique = []
    seen_hashes = set()
    for item in manifest_items:
        if item.get("download_status") != "success" or not item.get("sha256") or item["sha256"] in seen_hashes:
            continue
        seen_hashes.add(item["sha256"])
        successful_unique.append(item)
    canonical_count = len({x["canonical_url"] for x in manifest_items})
    strict_count = len(seen_hashes)
    manifest = {
        "schema_version": "1.0",
        "product_url": base_url,
        "shopify_product_id": str(product_js.get("id") or product_json.get("id")),
        "product_title": product_js.get("title") or product_json.get("title"),
        "variant_id": target_id,
        "sku": target_sku,
        "variant_title": matches[0].get("title"),
        "excluded_variants": job.get("excluded_variants", []),
        "generated_at": now_iso(),
        "source_position_count": len(manifest_items),
        "canonical_url_count": canonical_count,
        "strict_unique_count": strict_count,
        "effective_visual_count": None,
        "collection_mode": "gallery_all" if gallery_only else ("all_images" if all_images else "selected_positions"),
        "gallery_inventory": {
            "source": "shopify_product_images",
            "source_url": base_url + (".json" if product_json.get("images") else ".js"),
            "expected_count": gallery_expected,
            "discovered_count": len(gallery_positions),
            "downloaded_count": sum(x["section"] == "gallery" and x["download_status"] == "success" for x in manifest_items),
        },
        "unique_image_count": strict_count,
        "visual_review_status": "pending",
        "items": manifest_items,
        "failed_downloads": failed,
        "suspicious_mixed_assets": [],
        "source_snapshots": {
            "page": str(page_path.relative_to(product_root)).replace("\\", "/"),
            "product_json": str(json_path.relative_to(product_root)).replace("\\", "/"),
            "product_js": str(js_path.relative_to(product_root)).replace("\\", "/"),
            "sha256": {"page": page_hash, "product_json": json_hash, "product_js": js_hash},
        },
    }
    atomic_json(product_root / "图片资产清单.json", manifest)

    csv_path = product_root / "图片资产清单.csv"
    fields = [
        "source_position",
        "section",
        "section_position",
        "url",
        "canonical_url",
        "variant_scope",
        "filename",
        "sha256",
        "width",
        "height",
        "mime",
        "duplicate_of",
        "is_duplicate",
        "text_language_judgment",
        "download_status",
        "visual_duplicate_of",
    ]
    csv_tmp = csv_path.with_suffix(".csv.tmp")
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest_items)
    os.replace(csv_tmp, csv_path)

    sheets = build_contact_sheets(product_root, [x for x in successful_unique if x["canonical_url"] in selected_urls])
    append_evidence(
        product_root / "证据记录.jsonl",
        {
            "event": "official_product_snapshot",
            "source": base_url,
            "retrieved_date": datetime.now(timezone.utc).date().isoformat(),
            "shopify_product_id": manifest["shopify_product_id"],
            "target_variant": {"id": target_id, "sku": target_sku, "title": matches[0].get("title")},
            "available": matches[0].get("available"),
            "variant_count": len(variants),
            "snapshot_sha256": manifest["source_snapshots"]["sha256"],
        },
    )
    append_evidence(
        product_root / "证据记录.jsonl",
        {
            "event": "asset_collection",
            "source_position_count": len(manifest_items),
            "canonical_url_count": canonical_count,
            "strict_unique_count": strict_count,
            "failed_download_count": len(failed),
            "contact_sheets": sheets,
        },
    )
    if failed:
        update_state(product_root, "evidence_ready", ["evidence_ready"], "retry failed asset downloads after diagnosis")
    else:
        update_state(product_root, "assets_ready", ["evidence_ready", "assets_ready"], "screen every unique gallery image before selection; inspect adopted or uncertain files in detail; supplement only identified gaps")
    return {
        "product_id": product_root.name,
        "shopify_product_id": manifest["shopify_product_id"],
        "variant_id": target_id,
        "sku": target_sku,
        "positions": len(manifest_items),
        "canonical_urls": canonical_count,
        "strict_unique": strict_count,
        "failed": len(failed),
        "collection_mode": manifest["collection_mode"],
        "gallery_inventory": manifest["gallery_inventory"],
        "contact_sheets": sheets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--positions", nargs="+", type=int, help="supplement these specific positions; default downloads the whole product gallery")
    mode.add_argument("--discovery-only", action="store_true", help="discover URLs without image download or replacing the asset manifest")
    mode.add_argument("--all-images", action="store_true", help="explicit full download for a separately requested archive; not the listing default")
    args = parser.parse_args()
    result = collect(args.product_dir.resolve(), args.positions, args.all_images, args.discovery_only)
    print(json.dumps(result, ensure_ascii=False))
    return 12 if result.get("failed") else 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
