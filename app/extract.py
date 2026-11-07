"""로컬(무료) 분석: 페이지 렌더링, 텍스트 추출, 그림/표/도형 감지, 캡션 추출.

AI 호출 전에 여기서 최대한 걸러내서 크레딧을 아낍니다.
 - 그림/표/도형이 전혀 없는 텍스트 페이지는 AI로 보내지 않음
 - "그림 3. ...", "Table 2 ..." 같은 캡션은 정규식으로 무료 추출
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz
from PIL import Image

CAPTION_RE = re.compile(
    r"^\s*((?:그림|도표|표|차트|순서도|흐름도|Figure|Fig\.?|Table|Chart|Diagram|Flowchart)\s*[\dIVX]+[.:\-–)]?\s*.{0,120})",
    re.IGNORECASE | re.MULTILINE,
)
CAPTION_KIND = {
    "그림": "figure", "figure": "figure", "fig": "figure", "fig.": "figure",
    "표": "table", "table": "table",
    "도표": "chart", "차트": "chart", "chart": "chart",
    "순서도": "flowchart", "흐름도": "flowchart", "flowchart": "flowchart",
    "diagram": "diagram",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".img", ".heic"}


@dataclass
class PageInfo:
    page_no: int                    # 1부터
    width: int
    height: int
    text: str
    page_png: bytes                 # AI 전송용 원본 렌더 (PNG)
    n_images: int = 0               # 의미 있는 크기의 비트맵 수
    n_drawings: int = 0             # 벡터 도형 수 (플로우차트/차트 단서)
    n_tables: int = 0
    n_ink: int = 0                  # 펜 필기 주석(Ink annotation) 수 — 태블릿 필기 PDF
    is_image_file: bool = False
    captions: list[dict] = field(default_factory=list)

    @property
    def has_visual(self) -> bool:
        return (self.is_image_file or self.n_images > 0 or self.n_tables > 0 or self.n_drawings >= 15
                or self.n_ink > 0 or bool(self.captions))

    def local_items(self) -> list[dict]:
        """AI 없이 만들 수 있는 색인 항목."""
        items = [dict(c, source="local") for c in self.captions]
        kinds = {c["kind"] for c in self.captions}
        if self.n_tables and "table" not in kinds:
            items.append({"kind": "table", "title": "", "description": f"표 {self.n_tables}개 감지", "source": "local"})
        if (self.n_images or self.is_image_file) and not kinds & {"figure", "chart", "flowchart", "diagram"}:
            items.append({"kind": "figure", "title": "", "description": "그림 감지", "source": "local"})
        if self.n_ink:
            items.append({"kind": "handwriting", "title": "", "description": f"펜 필기 {self.n_ink}개 감지 (내용 검색은 AI 필요)",
                          "source": "local"})
        if self.n_drawings >= 40 and not kinds & {"flowchart", "chart", "diagram"}:
            items.append({"kind": "diagram", "title": "", "description": "벡터 도형(다이어그램/플로우차트 추정)", "source": "local"})
        return items


def to_pdf(path: Path) -> Path:
    """Office 파일을 LibreOffice로 PDF 변환 (설치되어 있을 때)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("Office 파일 변환에는 LibreOffice가 필요합니다.")
    out = Path(tempfile.mkdtemp())
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out), str(path)],
                   check=True, capture_output=True, timeout=300)
    pdf = out / (path.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"PDF 변환 실패: {path.name}")
    return pdf


def _captions(text: str) -> list[dict]:
    out, seen = [], set()
    for m in CAPTION_RE.finditer(text):
        line = m.group(1).strip()
        word = re.match(r"[^\s\d]+", line)
        key = word.group(0).lower().rstrip(".") if word else ""
        kind = CAPTION_KIND.get(key, CAPTION_KIND.get(key + ".", "figure"))
        if line not in seen:
            seen.add(line)
            out.append({"kind": kind, "title": line[:160], "description": ""})
    return out


def analyze(path: Path, dpi: int = 110) -> list[PageInfo]:
    """파일(PDF/이미지/Office)을 열어 페이지별 정보를 반환."""
    suffix = path.suffix.lower()
    if suffix in {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"}:
        path = to_pdf(path)
        suffix = ".pdf"

    if suffix != ".pdf":
        try:
            return [_analyze_image(path)]
        except Exception:
            pass  # 이미지가 아니면 PyMuPDF로 열어봄

    pages: list[PageInfo] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            area = page.rect.width * page.rect.height or 1
            page_notes: list[str] = []
            n_images = 0
            for info in page.get_image_info():
                x0, y0, x1, y1 = info["bbox"]
                if (x1 - x0) * (y1 - y0) / area > 0.03:      # 아이콘/로고 같은 작은 그림 무시
                    n_images += 1
            try:
                n_drawings = len(page.get_drawings())
            except Exception:
                n_drawings = 0
            try:
                n_tables = len(page.find_tables().tables)
            except Exception:
                n_tables = 0
            n_ink = 0
            for annot in page.annots() or []:
                if annot.type[0] == fitz.PDF_ANNOT_INK:
                    n_ink += 1
                elif annot.info.get("content"):          # 메모 주석의 글자는 무료로 검색되게
                    page_notes.append(annot.info["content"])
            text = (page.get_text("text") or "") + ("\n" + "\n".join(page_notes) if page_notes else "")
            pix = page.get_pixmap(dpi=dpi)
            pages.append(PageInfo(
                page_no=i + 1, width=pix.width, height=pix.height, text=text.strip(),
                page_png=pix.tobytes("png"), n_images=n_images, n_drawings=n_drawings,
                n_tables=n_tables, n_ink=n_ink, captions=_captions(text),
            ))
    return pages


def _analyze_image(path: Path) -> PageInfo:
    import io
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((2000, 2000))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return PageInfo(page_no=1, width=im.width, height=im.height, text="", page_png=buf.getvalue(),
                        is_image_file=True)


def save_renders(png: bytes, page_path: Path, thumb_path: Path, thumb_px: int = 360) -> None:
    """빠른 표시를 위해 미리 WebP로 저장 (전체 페이지 + 썸네일)."""
    import io
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGB")
        im.save(page_path, "WEBP", quality=80, method=4)
        im.thumbnail((thumb_px, thumb_px * 2))
        im.save(thumb_path, "WEBP", quality=70, method=4)


def shrink_for_ai(png: bytes, max_px: int) -> bytes:
    """AI로 보낼 이미지를 축소 + JPEG 압축 → 토큰/비용 절감."""
    import io
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=75)
        return buf.getvalue()
