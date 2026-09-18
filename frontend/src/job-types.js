/**
 * Mirrors the payload contracts in backend/app/job_types.py.
 *
 * This is duplication, knowingly: the alternative is a GET /job-types endpoint
 * the spec never called for. The cost is that a backend schema change needs a
 * matching edit HERE or the form will submit a payload the API rejects.
 *
 * The backend uses extra="forbid", so an invented field name is a 422, not a
 * silently ignored key — which at least makes drift loud rather than subtle.
 */
export const JOB_TYPES = {
  csv_process: {
    label: "CSV processing",
    description: "Parses a CSV and computes per-column statistics.",
    fields: [
      {
        name: "csv_text",
        label: "CSV text",
        type: "textarea",
        required: true,
        rows: 8,
        help: "Header row first.",
        placeholder: "name,age,score\nAlice,30,88.5\nBob,25,91.0",
      },
    ],
  },
  pdf_generate: {
    label: "PDF generation",
    description: "Renders a PDF document and stores it on the server.",
    fields: [
      { name: "title", label: "Title", type: "text", required: true, maxLength: 200 },
      {
        name: "body",
        label: "Body",
        type: "textarea",
        required: true,
        rows: 8,
        // The handler splits on blank lines — the form sends one plain string.
        help: "Leave a blank line between paragraphs.",
        placeholder: "First paragraph.\n\nSecond paragraph.",
      },
      {
        name: "author",
        label: "Author",
        type: "text",
        required: false,
        maxLength: 120,
        help: "Optional.",
      },
    ],
  },
};

// Matches MIN_PRIORITY / MAX_PRIORITY / DEFAULT_PRIORITY in schemas/job.py.
// Higher is more urgent, matching RabbitMQ's x-max-priority convention.
export const MIN_PRIORITY = 0;
export const MAX_PRIORITY = 10;
export const DEFAULT_PRIORITY = 5;
