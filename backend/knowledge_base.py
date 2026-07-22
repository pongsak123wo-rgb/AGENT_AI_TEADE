"""RAG pipeline: ingest PDFs into a local vector DB, retrieve relevant
chunks at analysis time. Uses Chroma's built-in embedding function so no
extra embedding API key is needed for ingestion/retrieval — only the
final reasoning step calls Claude.
"""
from __future__ import annotations

import io
from pathlib import Path

import chromadb
import fitz  # pymupdf
import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
_TESSDATA_DIR = Path(__file__).parent / "tessdata"

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
DB_DIR = Path(__file__).parent / "chroma_db"

_client = chromadb.PersistentClient(path=str(DB_DIR))
_collection = _client.get_or_create_collection("trading_knowledge")


def _chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return [c.strip() for c in chunks if c.strip()]


def _ocr_page(page: fitz.Page) -> str:
    pix = page.get_pixmap(dpi=200)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(
        img, lang="tha+eng", config=f"--tessdata-dir {_TESSDATA_DIR}"
    )


def ingest_pdf(path: Path) -> int:
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

    if chunks:
        _collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
    return len(chunks)


def ingest_all() -> dict:
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    results = {}
    for pdf_path in KNOWLEDGE_DIR.glob("*.pdf"):
        results[pdf_path.name] = ingest_pdf(pdf_path)
    return results


def ingest_text(source_name: str, text: str) -> int:
    """Ingest web-research snippets the same way as a PDF — same chunking,
    same collection, tagged by source so it's traceable later."""
    chunks = _chunk_text(text)
    ids = [f"web-{source_name}-{i}" for i in range(len(chunks))]
    metadatas = [{"source": source_name, "chunk": i, "origin": "web"} for i in range(len(chunks))]
    if chunks:
        _collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
    return len(chunks)


def retrieve(query: str, n_results: int = 4) -> list[str]:
    if _collection.count() == 0:
        return []
    res = _collection.query(query_texts=[query], n_results=min(n_results, _collection.count()))
    return res["documents"][0] if res["documents"] else []


def status() -> dict:
    return {"chunks_indexed": _collection.count(), "knowledge_dir": str(KNOWLEDGE_DIR)}
