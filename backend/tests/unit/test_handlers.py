"""Handlers run with no database, no Redis and no broker — the point of the
JobContext parameter added in Phase 9. Only pdf_generate touches the filesystem,
and it gets a tmp_path."""

import uuid

import pytest

from worker.handlers.csv_process import handle as csv_handle
from worker.handlers.pdf_generate import handle as pdf_handle
from worker.handlers.registry import HandlerError, JobContext


def ctx(tmp_path=None, attempt=1):
    return JobContext(
        job_id=uuid.uuid4(),
        attempt_number=attempt,
        storage_dir=tmp_path,
    )


# --------------------------- csv_process ---------------------------


def test_csv_process_computes_stats():
    result = csv_handle({"csv_text": "name,age\nAlice,30\nBob,25\n"}, ctx())
    assert result["row_count"] == 2
    assert result["column_count"] == 2
    assert result["columns"]["age"]["min"] == 25.0
    assert result["columns"]["age"]["max"] == 30.0
    assert result["columns"]["age"]["mean"] == 27.5


def test_csv_process_leaves_non_numeric_columns_without_stats():
    result = csv_handle({"csv_text": "name,age\nAlice,30\n"}, ctx())
    assert result["columns"]["name"] == {"non_null_count": 1}


def test_csv_process_treats_a_partly_numeric_column_as_text():
    result = csv_handle({"csv_text": "v\n1\nnot-a-number\n"}, ctx())
    assert "mean" not in result["columns"]["v"]


def test_csv_process_rejects_missing_field():
    with pytest.raises(HandlerError):
        csv_handle({}, ctx())


def test_csv_process_rejects_empty_csv():
    with pytest.raises(HandlerError):
        csv_handle({"csv_text": ""}, ctx())


def test_csv_process_rejects_non_string_csv():
    with pytest.raises(HandlerError):
        csv_handle({"csv_text": 12345}, ctx())


# --------------------------- pdf_generate ---------------------------


def test_pdf_generate_writes_a_real_pdf(tmp_path):
    context = ctx(tmp_path)
    result = pdf_handle({"title": "T", "body": "One.\n\nTwo.", "author": "A"}, context)

    out = tmp_path / result["file_name"]
    assert out.exists()
    assert out.name == f"{context.job_id}.pdf"
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF-")
    assert raw.rstrip().endswith(b"%%EOF")
    assert result["size_bytes"] == len(raw)
    assert result["paragraph_count"] == 2
    assert result["page_count"] == 1


def test_pdf_generate_author_is_optional(tmp_path):
    result = pdf_handle({"title": "T", "body": "Body."}, ctx(tmp_path))
    assert result["page_count"] == 1


def test_pdf_generate_rejects_missing_body(tmp_path):
    with pytest.raises(HandlerError):
        pdf_handle({"title": "T"}, ctx(tmp_path))


def test_pdf_generate_rejects_blank_title(tmp_path):
    with pytest.raises(HandlerError):
        pdf_handle({"title": "   ", "body": "Body."}, ctx(tmp_path))


def test_retrying_a_job_overwrites_its_own_output(tmp_path):
    # Named by job id, so attempt 2 must not leave attempt 1's file behind.
    context = ctx(tmp_path)
    pdf_handle({"title": "First", "body": "One."}, context)
    pdf_handle({"title": "Second", "body": "Two."}, context)
    assert len(list(tmp_path.glob("*.pdf"))) == 1
