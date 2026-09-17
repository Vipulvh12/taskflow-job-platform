import csv
import io

from worker.handlers.registry import HandlerError, register

__all__ = ["handle", "HandlerError"]


@register("csv_process")
def handle(payload: dict) -> dict:
    csv_text = payload.get("csv_text")
    if not csv_text or not isinstance(csv_text, str):
        raise HandlerError("payload.csv_text is required and must be a string.")

    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        raise HandlerError("CSV is empty.")

    header, data_rows = rows[0], rows[1:]
    columns: dict[str, dict] = {}

    for col_index, col_name in enumerate(header):
        values = [row[col_index] for row in data_rows if col_index < len(row)]
        numeric_values = []
        for v in values:
            try:
                numeric_values.append(float(v))
            except (TypeError, ValueError):
                pass

        is_numeric = len(values) > 0 and len(numeric_values) == len(values)
        stats = {"non_null_count": len(values)}
        if is_numeric:
            stats.update({
                "min": min(numeric_values),
                "max": max(numeric_values),
                "mean": sum(numeric_values) / len(numeric_values),
            })
        columns[col_name] = stats

    return {
        "row_count": len(data_rows),
        "column_count": len(header),
        "columns": columns,
    }
