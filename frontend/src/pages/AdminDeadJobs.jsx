import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listDeadJobs, retryDeadJob } from "../api/admin";
import { ApiError } from "../api/client";
import { JOB_TYPES } from "../job-types";

const PAGE_SIZE = 20;

export function AdminDeadJobs() {
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  // Per-row feedback: { [jobId]: { state: "pending" | "done" | "error", message } }
  const [rowState, setRowState] = useState({});

  // No polling: DEAD is terminal, so nothing on this page changes by itself.
  // The list is refetched only after a retry, which is the one thing that does.
  const load = useCallback(async () => {
    try {
      setData(await listDeadJobs({ page, pageSize: PAGE_SIZE }));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load dead jobs.");
    }
  }, [page]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleRetry(id) {
    setRowState((s) => ({ ...s, [id]: { state: "pending" } }));
    try {
      const res = await retryDeadJob(id);
      setRowState((s) => ({
        ...s,
        [id]: { state: "done", message: `Queued — fresh budget from attempt ${res.attempt_base + 1}` },
      }));
      await load();
    } catch (err) {
      // 409 (already retried, or no longer DEAD) and 502 (broker unavailable —
      // the job stays DEAD and can be retried again) both surface here.
      setRowState((s) => ({
        ...s,
        [id]: { state: "error", message: err instanceof ApiError ? err.message : "Retry failed." },
      }));
    }
  }

  const items = data?.items ?? [];

  return (
    <section>
      <header className="page-header">
        <h1>Dead jobs</h1>
      </header>
      <p className="muted">
        Jobs that exhausted their retries or failed permanently, across all users —
        highest priority first. Retrying grants a fresh retry budget; a job that died
        on bad input will die again.
      </p>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      {data && (
        <p className="muted">
          {data.total.toLocaleString()} dead job{data.total === 1 ? "" : "s"}
        </p>
      )}

      {data && items.length === 0 && <p>No dead jobs.</p>}

      {items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Priority</th>
              <th>Attempts</th>
              <th>Died</th>
              <th>Last error</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((job) => {
              const row = rowState[job.id];
              return (
                <tr key={job.id}>
                  <td>
                    <Link to={`/jobs/${job.id}`}>{JOB_TYPES[job.type]?.label ?? job.type}</Link>
                  </td>
                  <td>{job.priority}</td>
                  <td>{job.attempt_count}</td>
                  <td>{job.completed_at ? new Date(job.completed_at).toLocaleString() : "—"}</td>
                  <td className="error-cell">{job.last_error ?? "—"}</td>
                  <td>
                    <button
                      type="button"
                      className="secondary"
                      disabled={row?.state === "pending"}
                      onClick={() => handleRetry(job.id)}
                    >
                      {row?.state === "pending" ? "Retrying…" : "Retry"}
                    </button>
                    {row?.message && (
                      <div className={row.state === "error" ? "error-cell" : "muted"}>
                        {row.message}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {data && data.total_pages > 1 && (
        <p className="pager">
          <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <span className="muted">
            Page {data.page} of {data.total_pages.toLocaleString()}
          </span>
          <button
            type="button"
            disabled={page >= data.total_pages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </p>
      )}
    </section>
  );
}
