"""File storage inside MongoDB (GridFS).

Statement PDFs and invoices are saved in the database itself (collections
`files.files` and `files.chunks`), so nothing is written to the server's disk.
"""
import os
import tempfile
from contextlib import contextmanager
from urllib.parse import quote

import gridfs
from bson import ObjectId
from fastapi import HTTPException
from fastapi.responses import Response

from ..db import get_db

_fs = None


def fs() -> gridfs.GridFS:
    global _fs
    if _fs is None:
        db = get_db()
        if type(db).__module__.startswith("mongomock"):  # in-memory demo / tests
            from mongomock.gridfs import enable_gridfs_integration

            enable_gridfs_integration()
        _fs = gridfs.GridFS(db, collection="files")
    return _fs


def save_file(data: bytes, filename: str, content_type: str | None, **meta) -> ObjectId:
    return fs().put(data, filename=filename, content_type=content_type or "application/octet-stream", metadata=meta)


def delete_file(file_id) -> None:
    if file_id:
        try:
            fs().delete(ObjectId(file_id))
        except Exception:
            pass


def file_response(file_id, filename: str, content_type: str | None = None) -> Response:
    if not file_id:
        raise HTTPException(404, "File not found")
    try:
        g = fs().get(ObjectId(file_id))
    except gridfs.errors.NoFile:
        raise HTTPException(404, "File not found")
    data = g.read()
    ctype = content_type or getattr(g, "content_type", None) or "application/octet-stream"
    return Response(
        content=data,
        media_type=ctype,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@contextmanager
def temp_copy(data: bytes, suffix: str = ".pdf"):
    """Some libraries (pdfplumber) need a real file: write to a temp file that is
    deleted straight after use."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
