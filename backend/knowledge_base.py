"""RAG pipeline: ingest PDFs into a local vector DB, retrieve relevant
chunks at analysis time. Uses Chroma's built-in embedding function so no
extra embedding API key is needed for ingestion/retrieval — only the
final reasoning step calls Claude.
"""
from __future__ import annotations

import io
from pathlib import Path

import chromadb
try:
    import fitz  # pymupdf
except Exception:
    fitz = None

try:
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
except Exception:
    pytesseract = None
    Image = None
_TESSDATA_DIR = Path(__file__).parent / "tessdata"

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
DB_DIR = Path(__file__).parent / "chroma_db"

_client = chromadb.PersistentClient(path=str(DB_DIR))

def _get_collection():
    try:
        return _client.get_or_create_collection("trading_knowledge")
    except Exception:
        try:
            _client.delete_collection("trading_knowledge")
        except Exception:
            pass
        return _client.get_or_create_collection("trading_knowledge")


def _chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return [c.strip() for c in chunks if c.strip()]


def _ocr_page(page) -> str:
    if not fitz or not pytesseract or not Image:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(
            img, lang="tha+eng", config=f"--tessdata-dir {_TESSDATA_DIR}"
        )
    except Exception:
        return ""


def ingest_pdf(path: Path) -> int:
    if not fitz:
        return 0
    try:
        doc = fitz.open(str(path))
        page_texts = []
        for page in doc:
            text = page.get_text().strip()
            if not text:
                text = _ocr_page(page).strip()
            page_texts.append(text)
        full_text = "\n".join(page_texts)
        chunks = _chunk_text(full_text)

        ids = [f"{path.stem}-{i}" for i in range(len(chunks))]
        metadatas = [{"source": path.name, "chunk": i} for i in range(len(chunks))]
        col = _get_collection()
        if chunks and col:
            col.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        return len(chunks)
    except Exception:
        return 0


def ingest_all() -> int:
    if not KNOWLEDGE_DIR.exists():
        return 0
    total = 0
    for pdf in KNOWLEDGE_DIR.glob("*.pdf"):
        total += ingest_pdf(pdf)
    return total


def ingest_web(source_name: str, text: str) -> int:
    try:
        chunks = _chunk_text(text)
        ids = [f"web-{source_name}-{i}" for i in range(len(chunks))]
        metadatas = [{"source": source_name, "chunk": i, "origin": "web"} for i in range(len(chunks))]
        col = _get_collection()
        if chunks and col:
            col.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        return len(chunks)
    except Exception:
        return 0


def retrieve(query: str, n_results: int = 4) -> list[str]:
    try:
        col = _get_collection()
        if not col:
            return []
        cnt = col.count()
        if cnt == 0:
            return []
        res = col.query(query_texts=[query], n_results=min(n_results, cnt))
        return res["documents"][0] if res and res.get("documents") else []
    except Exception:
        return []


def status() -> dict:
    try:
        col = _get_collection()
        cnt = col.count() if col else 0
    except Exception:
        cnt = 0
    return {"chunks_indexed": cnt, "knowledge_dir": str(KNOWLEDGE_DIR)}
