"""SQLite 색인 저장소. 검색은 수 ms 내에 끝나도록 단순 구조 + 인덱스 사용."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files (
    file_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    mime_type    TEXT,
    path         TEXT,
    web_link     TEXT,
    version_key  TEXT,          -- md5Checksum 또는 modifiedTime: 변경 없으면 재처리 생략
    page_count   INTEGER DEFAULT 0,
    indexed_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pages (
    page_key     TEXT PRIMARY KEY,   -- {file_id}_{page_no}
    file_id      TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    page_no      INTEGER NOT NULL,
    width        INTEGER,
    height       INTEGER,
    text         TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    page_key     TEXT NOT NULL REFERENCES pages(page_key) ON DELETE CASCADE,
    file_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- figure | table | flowchart | chart | diagram | photo | screenshot
    title        TEXT,
    description  TEXT,
    keywords     TEXT,
    source       TEXT,               -- local | gemini | openai
    search_text  TEXT                -- 소문자 결합 텍스트 (검색용)
);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_page ON items(page_key);
CREATE INDEX IF NOT EXISTS idx_pages_file ON pages(file_id);
CREATE TABLE IF NOT EXISTS vision_cache (
    img_hash     TEXT PRIMARY KEY,   -- 같은 그림은 다시 AI에 보내지 않음
    provider     TEXT,
    result_json  TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS thesaurus (
    term         TEXT PRIMARY KEY,   -- 정규화된 용어
    variants     TEXT                -- JSON 배열: [원래 표기, 동의어...]
);
"""


def img_version(version_key: str | None) -> str:
    """이미지 주소에 붙일 짧은 버전값: 파일이 바뀌어 다시 그리면 브라우저 캐시 대신 새 이미지를 받음."""
    import hashlib
    return hashlib.md5((version_key or "").encode()).hexdigest()[:8]


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # 스레드마다 연결을 따로 씀: 색인(쓰기) 중에도 화면 검색(읽기)이 충돌 없이 동작 (WAL 모드)
        self._local = threading.local()
        self.lock = threading.Lock()      # 쓰기는 한 번에 하나씩
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "abs_path" not in cols:        # 이전 버전 DB 업그레이드
            with self.lock, self.conn:
                self.conn.execute("ALTER TABLE files ADD COLUMN abs_path TEXT")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    # ── meta ──
    def set_meta(self, k: str, v: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))

    def get_meta(self, k: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    # ── files ──
    def page_keys(self, file_id: str) -> set[str]:
        return {r["page_key"] for r in self.conn.execute("SELECT page_key FROM pages WHERE file_id=?", (file_id,))}

    def file_version(self, file_id: str) -> str | None:
        row = self.conn.execute("SELECT version_key FROM files WHERE file_id=?", (file_id,)).fetchone()
        return row["version_key"] if row else None

    def all_file_ids(self) -> set[str]:
        return {r["file_id"] for r in self.conn.execute("SELECT file_id FROM files")}

    def drive_api_files(self) -> set[str]:
        return {r["file_id"] for r in self.conn.execute(
            "SELECT file_id FROM files WHERE abs_path IS NULL AND COALESCE(mime_type,'') NOT LIKE 'local%'")}

    def files_under(self, root: str) -> set[str]:
        """내 PC 폴더 색인: 이 폴더 안에 있던 파일(+경로 정보가 없는 예전 버전 파일)."""
        root = root.rstrip("\\/") + "\\" if "\\" in root else root.rstrip("/") + "/"
        out = set()
        for r in self.conn.execute("SELECT file_id, abs_path, mime_type FROM files"):
            ap = r["abs_path"]
            if ap is None:
                if (r["mime_type"] or "").startswith("local"):
                    out.add(r["file_id"])
            elif ap.startswith(root):
                out.add(r["file_id"])
        return out

    def delete_file(self, file_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM items WHERE file_id=?", (file_id,))
            self.conn.execute("DELETE FROM pages WHERE file_id=?", (file_id,))
            self.conn.execute("DELETE FROM files WHERE file_id=?", (file_id,))

    def save_file(self, meta: dict[str, Any], pages: list[dict[str, Any]]) -> None:
        """한 파일의 모든 페이지/항목을 트랜잭션 하나로 교체 저장."""
        fid = meta["file_id"]
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM items WHERE file_id=?", (fid,))
            self.conn.execute("DELETE FROM pages WHERE file_id=?", (fid,))
            self.conn.execute(
                "INSERT OR REPLACE INTO files(file_id, name, mime_type, path, web_link, version_key, page_count,"
                " abs_path) VALUES(?,?,?,?,?,?,?,?)",
                (fid, meta["name"], meta.get("mime_type"), meta.get("path"), meta.get("web_link"),
                 meta.get("version_key"), len(pages), meta.get("_local_key")),
            )
            for p in pages:
                self.conn.execute(
                    "INSERT INTO pages(page_key, file_id, page_no, width, height, text) VALUES(?,?,?,?,?,?)",
                    (p["page_key"], fid, p["page_no"], p.get("width"), p.get("height"), p.get("text", "")),
                )
                for it in p.get("items", []):
                    kw = it.get("keywords") or []
                    if isinstance(kw, list):
                        kw = ", ".join(map(str, kw))
                    search_text = " ".join(
                        str(x) for x in (it.get("kind"), it.get("title"), it.get("description"), kw,
                                         meta["name"], meta.get("path"), p.get("text", "")[:4000]) if x
                    ).lower()
                    self.conn.execute(
                        "INSERT INTO items(page_key, file_id, kind, title, description, keywords, source, search_text)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (p["page_key"], fid, it["kind"], it.get("title"), it.get("description"), kw,
                         it.get("source", "local"), search_text),
                    )

    # ── vision cache ──
    def cache_get(self, img_hash: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT result_json FROM vision_cache WHERE img_hash=?", (img_hash,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def cache_put(self, img_hash: str, provider: str, result: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO vision_cache(img_hash, provider, result_json) VALUES(?,?,?)",
                (img_hash, provider, json.dumps(result, ensure_ascii=False)),
            )

    # ── search ──
    def search(self, q: str = "", kind: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
        where, args = [], []
        for term in q.lower().split():
            term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("i.search_text LIKE ? ESCAPE '\\'")
            args.append(f"%{term}%")
        if kind:
            where.append("i.kind = ?")
            args.append(kind)
        sql = (
            "SELECT i.id, i.kind, i.title, i.description, i.keywords, i.source, i.page_key,"
            " p.page_no, p.width, p.height, f.name AS file_name, f.path, f.web_link, f.page_count, f.file_id,"
            " f.version_key"
            " FROM items i JOIN pages p ON p.page_key=i.page_key JOIN files f ON f.file_id=i.file_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY f.name, p.page_no, i.id LIMIT ? OFFSET ?"
        )
        args += [limit, offset]
        return [dict(r) for r in self.conn.execute(sql, args)]

    def page(self, page_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT p.*, f.name AS file_name, f.web_link, f.page_count, f.path FROM pages p"
            " JOIN files f ON f.file_id=p.file_id WHERE p.page_key=?", (page_key,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = [dict(r) for r in self.conn.execute(
            "SELECT kind, title, description, keywords, source FROM items WHERE page_key=? ORDER BY id", (page_key,))]
        return d

    def export_pages(self) -> list[dict]:
        """정적 사이트용: 페이지별 항목 + 검색 텍스트."""
        out: dict[str, dict] = {}
        rows = self.conn.execute(
            "SELECT p.page_key, p.page_no, p.width, p.height, p.text, f.name AS file_name, f.path, f.web_link,"
            " f.page_count, f.version_key FROM pages p JOIN files f ON f.file_id=p.file_id ORDER BY f.name, p.page_no")
        for r in rows:
            out[r["page_key"]] = {
                "page_key": r["page_key"], "page_no": r["page_no"], "width": r["width"], "height": r["height"],
                "file_name": r["file_name"], "path": r["path"], "web_link": r["web_link"],
                "page_count": r["page_count"], "v": img_version(r["version_key"]),
                "t": " ".join(x for x in (r["file_name"], r["path"], (r["text"] or "")[:1000]) if x).lower(),
                "items": []}
        for r in self.conn.execute("SELECT page_key, kind, title, description, keywords, source FROM items ORDER BY id"):
            p = out.get(r["page_key"])
            if p is None:
                continue
            it = {k: r[k] for k in ("kind", "title", "description", "keywords", "source")}
            it["s"] = " ".join(str(v) for v in (r["kind"], r["title"], r["description"], r["keywords"]) if v).lower()
            p["items"].append(it)
        return [p for p in out.values() if p["items"]]

    # ── 동의어 사전 ──
    def thesaurus_terms(self) -> set[str]:
        return {r["term"] for r in self.conn.execute("SELECT term FROM thesaurus")}

    def thesaurus_save(self, entries: dict[str, list[str]]) -> None:
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO thesaurus(term, variants) VALUES(?, ?)",
                                  [(k, json.dumps(v, ensure_ascii=False)) for k, v in entries.items() if k])

    def thesaurus_groups(self) -> list[list[str]]:
        return [json.loads(r["variants"]) for r in self.conn.execute("SELECT variants FROM thesaurus")]

    def stats(self) -> dict:
        c = self.conn
        return {
            "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "pages": c.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "by_kind": {r[0]: r[1] for r in c.execute("SELECT kind, COUNT(*) FROM items GROUP BY kind")},
            "by_source": {r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM items GROUP BY source")},
        }
