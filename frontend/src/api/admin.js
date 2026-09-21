import { apiFetch } from "./client.js";

/** GET /admin/jobs/dead -> { items, page, page_size, total, total_pages } */
export function listDeadJobs({ page = 1, pageSize = 20 } = {}) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiFetch(`/admin/jobs/dead?${params.toString()}`);
}

/** POST /admin/jobs/{id}/retry -> { job_id, status, attempt_base } */
export function retryDeadJob(id) {
  return apiFetch(`/admin/jobs/${id}/retry`, { method: "POST" });
}
