from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from worker.handlers.registry import HandlerError, JobContext, register

# Written into the PDF's own metadata so a file recovered from disk can be
# traced back to the job that produced it.
PRODUCER = "TaskFlow"


@register("pdf_generate")
def handle(payload: dict, context: JobContext) -> dict:
    title = payload.get("title")
    body = payload.get("body")
    author = payload.get("author")

    # The API validates payload shape at submission, but a handler must still
    # check: a row can be edited directly, and a replayed message may predate
    # a schema change. Cheap defence in depth.
    if not isinstance(title, str) or not title.strip():
        raise HandlerError("payload.title is required and must be a non-empty string.")
    if not isinstance(body, str) or not body.strip():
        raise HandlerError("payload.body is required and must be a non-empty string.")

    context.storage_dir.mkdir(parents=True, exist_ok=True)
    # Named by job id, so a retry of the same job overwrites its own previous
    # output rather than accumulating orphaned files per attempt.
    out_path = context.storage_dir / f"{context.job_id}.pdf"

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        title=title,
        author=author or PRODUCER,
        subject=f"TaskFlow job {context.job_id}",
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
    )

    story = [Paragraph(title, styles["Title"]), Spacer(1, 6 * mm)]
    if author:
        story += [Paragraph(f"by {author}", styles["Italic"]), Spacer(1, 4 * mm)]

    paragraphs = [block.strip() for block in body.split("\n\n") if block.strip()]
    for block in paragraphs:
        story += [Paragraph(block.replace("\n", "<br/>"), styles["BodyText"]),
                  Spacer(1, 3 * mm)]

    page_count_holder = {"pages": 0}

    def _count_page(canvas, _doc):
        page_count_holder["pages"] = canvas.getPageNumber()

    try:
        doc.build(story, onLaterPages=_count_page, onFirstPage=_count_page)
    except Exception as exc:
        # Anything reportlab rejects that the checks above didn't catch (an
        # unclosed tag in body text, say) is still the payload's fault, so it
        # is permanent — retrying identical input would fail identically.
        raise HandlerError(f"PDF generation failed: {type(exc).__name__}: {exc}") from exc

    return {
        # Relative to storage_dir: the absolute path differs between the host
        # and a container, so storing it would bake in one environment.
        "file_name": out_path.name,
        "size_bytes": out_path.stat().st_size,
        "page_count": page_count_holder["pages"],
        "paragraph_count": len(paragraphs),
        "title": title,
    }
