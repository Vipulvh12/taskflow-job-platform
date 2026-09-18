import { apiFetch } from "./client.js";

/**
 * POST /jobs -> { job_id, status }
 *
 * `idempotencyKey` is optional. The API also accepts it as an Idempotency-Key
 * header; the body field is used here so there is one place to look.
 */
export function submitJob({ type, payload, priority, idempotencyKey }) {
  const body = { type, payload, priority };
  if (idempotencyKey) body.idempotency_key = idempotencyKey;
  return apiFetch("/jobs", { method: "POST", body: JSON.stringify(body) });
}

/** GET /jobs -> { items, page, page_size, total, total_pages } */
export function listJobs({ status, type, page = 1, pageSize = 20 } = {}) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (status) params.set("status", status);
  if (type) params.set("type", type);
  return apiFetch(`/jobs?${params.toString()}`);
}

/** GET /jobs/{id} -> the full record, including its `attempts` array. */
export function getJob(id) {
  return apiFetch(`/jobs/${id}`);
}
