"""학습 앱 연동용 내보내기 (PC에서 실행 → NAS의 EXPORT_DIR/api/ 에 저장).

NAS(DS216play)는 PDF를 열거나 그림을 자를 수 없으므로 무거운 일은 전부 PC에서 미리 해 둠.
 - api/pages.jsonl   : NAS 검색 API가 읽는 페이지 목록 (한 줄 = 한 페이지)
 - api/emb.bin       : pages.jsonl 순서의 의미 검색 벡터 (int8)
 - api/pages/*.jpg   : 페이지 이미지 1200px (야간 골격 생성 때 Gemini에 첨부)
 - api/export/        : 드라이브 '_drive_index_export'와 똑같은 모양 (NAS Cloud Sync가 이 폴더를 그대로 올림)
     *.jsonl          : 전체 색인 (책별, 파일당 40MB 이하)
     figs/*.jpg       : 그림·표만 잘라 낸 이미지 800px, JPEG 70
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path

from PIL import Image

from .config import settings
from .db import DB

log = logging.getLogger(__name__)

PAGE_W, FIG_W = 1200, 800
FIGS = Path("export") / "figs"          # api/ 아래 그림 조각 폴더
PAGE_Q, FIG_Q = 75, 70
MAX_JSONL = 40 * 1024 * 1024
KIND_KO = {"table": "표", "handwriting": "본문", "text": "본문"}
NO_FIG = ("handwriting", "text")        # 그림 조각을 만들지 않는 항목 (손글씨·글 페이지)       # 나머지(그림·플로우차트·차트·도해·사진)는 '그림'
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def kind_ko(kind: str) -> str:
    return KIND_KO.get(kind, "그림")


def book_of(file_name: str) -> str:
    return Path(file_name).stem


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")
    return s[:120] or "book"


# ── 그림·표 위치 찾기 (AI 없이, 무료) ─────────────────────────────
def _trim_box(im: Image.Image, pad: int = 12) -> tuple[int, int, int, int]:
    """흰 여백을 뺀 내용 영역."""
    g = im.convert("L")
    g.thumbnail((600, 600))
    sx, sy = im.width / g.width, im.height / g.height
    box = g.point(lambda v: 255 if v < 200 else 0).getbbox()
    if not box:
        return 0, 0, im.width, im.height
    x0, y0, x1, y1 = box
    return (max(0, int(x0 * sx) - pad), max(0, int(y0 * sy) - pad),
            min(im.width, int(x1 * sx) + pad), min(im.height, int(y1 * sy) + pad))


def find_regions(page) -> list[tuple[object, str]]:
    """PDF 페이지에서 그림·도형·표가 있는 사각형들 (위→아래 순).

    글자로 된 PDF(벡터 그림·표, 삽입 이미지)는 정확히 잡힘. 통째로 스캔한 페이지는 빈 목록 → 호출한 쪽에서
    여백만 뺀 페이지 전체를 씀."""
    import pymupdf as fitz
    pr = page.rect
    W, H = pr.width, pr.height
    regs: list[list] = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & pr
        if r.is_empty or r.get_area() > 0.8 * W * H:       # 페이지 전체 스캔 이미지는 제외
            continue
        if r.width >= 0.12 * W and r.height >= 0.06 * H:
            regs.append([r, "img"])
    try:
        draws = page.get_drawings()
    except Exception:
        draws = []
    if 4 <= len(draws) <= 6000:
        try:
            for r in page.cluster_drawings(drawings=draws):
                if r.width >= 0.15 * W and r.height >= 0.05 * H:
                    regs.append([fitz.Rect(r) & pr, "draw"])
        except Exception:
            pass
        try:
            for t in page.find_tables().tables:
                regs.append([fitz.Rect(t.bbox) & pr, "table"])
        except Exception:
            pass
    # 겹치는 사각형은 하나로 (표 테두리와 도형 묶음이 겹치는 경우)
    merged: list[list] = []
    for r, t in sorted(regs, key=lambda x: (x[0].y0, x[0].x0)):
        for m in merged:
            if m[0].intersects(r):
                m[0] = m[0] | r
                if t == "table":
                    m[1] = "table"
                break
        else:
            merged.append([fitz.Rect(r), t])
    return [(r, t) for r, t in sorted(merged, key=lambda x: (x[0].y0, x[0].x0))]


def assign_regions(kinds: list[str], regions: list[tuple[object, str]]) -> list[object | None]:
    """항목(위→아래로 나열됐다고 봄)마다 사각형 하나씩. 표 항목은 표 영역부터."""
    tables = [r for r, t in regions if t == "table"]
    others = [r for r, t in regions if t != "table"]
    out: list[object | None] = []
    for k in kinds:
        if k in NO_FIG:
            out.append(None)
            continue
        pool = tables if k == "table" and tables else (others if others else tables)
        out.append(pool.pop(0) if pool else None)
    return out


def _fit_width(im: Image.Image, w: int) -> Image.Image:
    if im.width == w:
        return im
    h = max(1, round(im.height * w / im.width))
    return im.resize((w, h), Image.LANCZOS)


def _save_jpg(im: Image.Image, path: Path, q: int) -> None:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=q, optimize=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)


class _Renderer:
    """원본 파일을 열어 페이지를 고해상도(가로 1600px)로 그림. 같은 파일은 한 번만 엶."""

    def __init__(self):
        self.path, self.doc = None, None

    def close(self):
        if self.doc is not None:
            self.doc.close()
        self.path, self.doc = None, None

    def render(self, abs_path: str | None, page_no: int, page_key: str):
        """(PIL 이미지, 그림 사각형들, 포인트→픽셀 배율)."""
        if abs_path and Path(abs_path).exists():
            ext = Path(abs_path).suffix.lower()
            if ext in IMG_EXTS:
                im = Image.open(abs_path).convert("RGB")
                im.thumbnail((2400, 2400))
                return im, [], 1.0
            if ext == ".pdf":
                import pymupdf as fitz
                if self.path != abs_path:
                    self.close()
                    self.doc, self.path = fitz.open(abs_path), abs_path
                page = self.doc[page_no - 1]
                scale = 1600 / max(1.0, page.rect.width)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples) if pix.n == 3 \
                    else Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                regs = find_regions(page)
                if page_no % 20 == 0:
                    fitz.TOOLS.store_shrink(100)
                return im, regs, scale
        # 원본이 없으면(Drive API 모드·Office 파일) 색인 때 만든 화면용 이미지로 대신함
        p = settings.pages_dir / f"{page_key}.webp"
        if p.exists():
            return Image.open(p).convert("RGB"), [], 1.0
        return None, [], 1.0


def make_assets(page: dict, out_api: Path, rend: _Renderer) -> list[str | None]:
    """페이지 이미지 1장 + 항목별 그림 조각. 항목마다 그림 파일 이름(없으면 None) 반환."""
    im, regs, scale = rend.render(page.get("abs_path"), page["page_no"], page["page_key"])
    kinds = [it["kind"] for it in page["items"]]
    if im is None:
        return [None] * len(kinds)
    key = page["page_key"]
    _save_jpg(_fit_width(im, PAGE_W) if im.width > PAGE_W else im, out_api / "pages" / f"{key}.jpg", PAGE_Q)
    rects = assign_regions(kinds, regs)
    names: list[str | None] = []
    whole = None
    for i, (k, r) in enumerate(zip(kinds, rects)):
        if k in NO_FIG:
            names.append(None)
            continue
        if r is not None:
            # 캡션(그림 제목)이 위·아래에 붙어 있도록 조금 넉넉하게
            x0, y0 = max(0, int((r.x0 - 8) * scale)), max(0, int((r.y0 - 22) * scale))
            x1, y1 = min(im.width, int((r.x1 + 8) * scale)), min(im.height, int((r.y1 + 22) * scale))
            crop = im.crop((x0, y0, x1, y1))
            crop = crop.crop(_trim_box(crop, 6))
        else:
            if whole is None:
                whole = im.crop(_trim_box(im))
            crop = whole
        name = f"{key}_{i}.jpg"
        _save_jpg(_fit_width(crop, FIG_W), out_api / FIGS / name, FIG_Q)
        names.append(name)
    return names


# ── 내보내기 본체 ────────────────────────────────────────────────
def _collect(db: DB) -> list[dict]:
    """화면과 같은 걸러진 페이지 목록(build_payload) + 원문 글자 + 요약."""
    from .export import build_payload
    data, texts, _ = build_payload(db)
    raw = {p["page_key"]: p for p in db.export_pages(with_text=True)}
    out = []
    for key, fi, page_no, items in data["pages"]:
        f = data["files"][fi]
        r = raw.get(key) or {}
        t = texts.get(key) or ["", [], "", [], []]
        details = t[1]
        out.append({
            "page_key": key, "file_name": f[0], "path": f[1], "page_count": f[3], "v": f[4],
            "page_no": page_no, "abs_path": r.get("abs_path"),
            "text": " ".join((r.get("text") or "").split())[:3000],
            "summary": t[2] or "", "concepts": t[3] or [], "questions": t[4] or [],
            "items": [{"kind": k, "title": ti, "description": (details[i][0] if i < len(details) else "") or d,
                       "keywords": (details[i][1] if i < len(details) else "") or ""}
                      for i, (k, ti, d, _src) in enumerate(items)],
        })
    return out


def _write_jsonl_split(rows_by_book: dict[str, list[dict]], dst: Path) -> list[str]:
    dst.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for book, rows in sorted(rows_by_book.items()):
        base, part, buf, size = safe_name(book), 1, [], 0

        def flush():
            name = f"{base}.jsonl" if part == 1 else f"{base}_{part}.jsonl"
            tmp = dst / (name + ".part")
            tmp.write_bytes(b"".join(buf))
            tmp.replace(dst / name)
            written.append(name)

        for r in rows:
            line = (json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if buf and size + len(line) > MAX_JSONL:
                flush()
                part, buf, size = part + 1, [], 0
            buf.append(line)
            size += len(line)
        if buf:
            flush()
    for old in dst.glob("*.jsonl"):
        if old.name not in written:
            old.unlink()
    return written


def export_api(db: DB, out_dir: Path, budget_s: float | None = None) -> dict:
    """EXPORT_DIR/api/ 갱신. 바뀐 페이지의 이미지만 새로 만듦. budget_s 초가 지나면 이미지 만들기는
    다음 번으로 미룸 (색인 중 10분마다 하는 중간 내보내기가 길어지지 않게)."""
    from . import embed
    started = time.time()
    api = Path(out_dir) / "api"
    old_figs = api / "figs"
    if old_figs.is_dir() and not (api / FIGS).exists():      # 예전 위치(api/figs) → 새 위치로 옮김 (다시 만들 필요 없음)
        (api / "export").mkdir(parents=True, exist_ok=True)
        old_figs.rename(api / FIGS)
    for d in ("pages", FIGS, "export"):
        (api / d).mkdir(parents=True, exist_ok=True)
    man_path = api / "assets.json"
    try:
        manifest: dict = json.loads(man_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    pages = _collect(db)
    rend = _Renderer()
    made = pending = 0
    try:
        for p in sorted(pages, key=lambda x: (x["abs_path"] or "", x["page_no"])):   # 같은 파일끼리 → 한 번만 엶
            sig = f"{p['v']}|{len(p['items'])}|{','.join(it['kind'] for it in p['items'])}"
            m = manifest.get(p["page_key"])
            if m and m.get("sig") == sig and (api / "pages" / f"{p['page_key']}.jpg").exists():
                continue
            if budget_s is not None and time.time() - started > budget_s:
                pending += 1
                continue
            try:
                figs = make_assets(p, api, rend)
            except Exception as e:          # 한 페이지 실패가 전체를 멈추지 않게
                log.warning("그림 자르기 실패 %s: %s", p["page_key"], e)
                figs = [None] * len(p["items"])
            manifest[p["page_key"]] = {"sig": sig, "figs": figs}
            made += 1
            if made % 200 == 0:
                man_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    finally:
        rend.close()
    live = {p["page_key"] for p in pages}
    for k in list(manifest):
        if k not in live:                                      # 지워진 페이지의 이미지 정리
            for f in [f"pages/{k}.jpg"] + [f"{FIGS.as_posix()}/{n}" for n in manifest[k].get("figs") or [] if n]:
                try:
                    (api / f).unlink()
                except OSError:
                    pass
            del manifest[k]
    tmp = man_path.with_name("assets.json.part")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    tmp.replace(man_path)

    # 1) NAS 검색 API용 pages.jsonl + emb.bin (같은 순서)
    lines, keys, by_book = [], [], {}
    for p in pages:
        m = manifest.get(p["page_key"]) or {}
        figs = m.get("figs") or []
        has_img = (api / "pages" / f"{p['page_key']}.jpg").exists()
        book = book_of(p["file_name"])
        its = []
        for i, it in enumerate(p["items"]):
            fig = figs[i] if i < len(figs) else None
            its.append([it["kind"], it["title"] or "", it["description"] or "", it["keywords"] or "", fig])
            by_book.setdefault(book, []).append({
                "book": book, "page": p["page_no"], "page_key": p["page_key"], "kind": kind_ko(it["kind"]),
                "title": it["title"] or "", "summary": p["summary"], "text": p["text"][:800],
                "page_img": f"pages/{p['page_key']}.jpg" if has_img else None,
                "fig_img": f"figs/{fig}" if fig else None,
            })
        lines.append({"k": p["page_key"], "b": book, "f": p["file_name"], "path": p["path"], "n": p["page_no"],
                      "pc": p["page_count"], "it": its, "s": p["summary"], "c": p["concepts"],
                      "q": p["questions"], "t": p["text"], "img": has_img})
        keys.append(p["page_key"])
    blob = db.embedding_blob(keys, embed.DIM)
    (api / "emb.bin.part").write_bytes(blob)
    (api / "emb.bin.part").replace(api / "emb.bin")
    from . import thesaurus
    meta = {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "pages": len(lines),
            "embed": {"dim": embed.DIM, "model": settings.embed_model, "ok": any(blob)},
            "thesaurus": thesaurus.groups(db), "pending_images": pending}
    (api / "meta.json.part").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (api / "meta.json.part").replace(api / "meta.json")
    body = "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in lines)
    (api / "pages.jsonl.part").write_text(body, encoding="utf-8")
    (api / "pages.jsonl.part").replace(api / "pages.jsonl")    # 마지막에 교체 → NAS API가 이걸 보고 다시 읽음

    # 2) 드라이브용 전체 색인 (책별 JSONL)
    files = _write_jsonl_split(by_book, api / "export")
    # 3) 학습앱이 요청한 페이지 전체 이미지 (주제 비교 탭) — 색인에 없는 글 페이지도 PDF에서 그림
    left = None if budget_s is None else max(0.0, budget_s - (time.time() - started))
    req = export_requested_pages(db, api / "export" / "pages", left)
    return {"pages": len(lines), "images_made": made, "images_pending": pending + req.get("pending", 0),
            "jsonl": files, "page_request": req}


REQ_PX, REQ_Q = 1400, 70          # 긴 변 1400px, JPEG 품질 70 (한 장 150KB 안팎)


def request_path() -> Path:
    """학습앱이 드라이브 gemini 폴더에 두는 페이지 요청 파일 (PC는 Google Drive for desktop 경로로 읽음)."""
    return settings.questions_path.parent / "_페이지요청.json"


def export_requested_pages(db: DB, dst: Path, budget_s: float | None = None) -> dict:
    """_페이지요청.json 의 page_keys 를 dst/<page_key>.jpg 로 (이미 있으면 건너뜀, 요청에서 빠진 건 지움).

    결과는 요청 파일 옆 _페이지요청_결과.json 에 남김 → 학습앱이 확인."""
    import pymupdf as fitz
    path = request_path()
    try:
        req = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    keys = [str(k) for k in (req.get("page_keys") if isinstance(req, dict) else req) or []]
    dst.mkdir(parents=True, exist_ok=True)
    started = time.time()
    want = set(keys)
    made = have = 0
    missing, pending = [], 0
    docs: dict[str, object] = {}
    try:
        for key in sorted(keys):                       # 같은 책끼리 → 한 번만 엶
            out = dst / f"{key}.jpg"
            if out.exists() and out.stat().st_size > 0:
                have += 1
                continue
            if budget_s is not None and time.time() - started > budget_s:
                pending += 1
                continue
            fid, _, no = key.rpartition("_")
            ap = db.file_abs_path(fid) if fid else None
            if not ap or not no.isdigit() or not Path(ap).exists() or Path(ap).suffix.lower() != ".pdf":
                missing.append(key)
                continue
            try:
                doc = docs.get(ap)
                if doc is None:
                    for d in docs.values():
                        d.close()
                    docs.clear()
                    doc = docs[ap] = fitz.open(ap)
                n = int(no)
                if not 1 <= n <= doc.page_count:
                    missing.append(key)
                    continue
                page = doc[n - 1]
                zoom = REQ_PX / max(page.rect.width, page.rect.height, 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples) if pix.n == 3 \
                    else Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                _save_jpg(im, out, REQ_Q)
                made += 1
                if made % 50 == 0:
                    fitz.TOOLS.store_shrink(100)
            except Exception as e:                     # 깨진 쪽 하나가 전체를 멈추지 않게
                log.warning("요청 페이지 실패 %s: %s", key, e)
                missing.append(key)
    finally:
        for d in docs.values():
            d.close()
    removed = 0
    for f in dst.glob("*.jpg"):                        # 요청 목록에서 빠진 페이지 정리
        if f.stem not in want:
            f.unlink()
            removed += 1
    res = {"requested": len(keys), "made": made, "existing": have, "pending": pending,
           "missing": len(missing), "removed": removed}
    try:
        report = dict(res, at=time.strftime("%Y-%m-%d %H:%M"), missing_keys=missing[:200],
                      folder="_drive_index_export/pages", done=pending == 0)
        tmp = path.with_name("_페이지요청_결과.json.part")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path.with_name("_페이지요청_결과.json"))
    except OSError as e:
        log.warning("페이지 요청 결과 기록 실패: %s", e)
    return res


if __name__ == "__main__":      # python -m app.apiexport [EXPORT_DIR]
    import sys
    logging.basicConfig(level=logging.INFO)
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(settings.export_dir)
    print(export_api(DB(settings.db_path), target))
