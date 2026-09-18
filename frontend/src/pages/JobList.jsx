import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listJobs } from "../api/jobs";
import { StatusBadge } from "../components/StatusBadge";
import { hasActiveJobs } from "../job-status";
import { JOB_TYPES } from "../job-types";

const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 15000; // still checking, just not hammering, while quiet
const PAGE_SIZE = 20;

export function JobList() {
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let timeoutId = null;

    // A self-scheduling tick, not setInterval. The cadence is recomputed from
    // each response, which is what makes polling *resumable*: an idle list
    // still checks every 15s, so a job submitted in another tab shows up
    // without a reload. Stopping outright once everything settled would leave
    // the page silent forever.
    async function tick() {
      let nextDelay = IDLE_POLL_MS;
      try {
        const fresh = await listJobs({ page, pageSize: PAGE_SIZE });
        if (cancelled) return;
        setData(fresh);
        setError(null);
        nextDelay = hasActiveJobs(fresh.items) ? ACTIVE_POLL_MS : IDLE_POLL_MS;
      } catch {
        if (!cancelled) setError("Could not load jobs.");
      } finally {
        // Guarded: a cancelled effect must not queue another tick.
        if (!cancelled) timeoutId = setTimeout(tick, nextDelay);
      }
    }

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [page]);

  const refreshNow = useCallback(async () => {
    try {
      setData(await listJobs({ page, pageSize: PAGE_SIZE }));
      setError(null);
    } catch {
      setError("Could not load jobs.");
    }
  }, [page]);

  const jobs = data?.items ?? [];
  const active = hasActiveJobs(jobs);

  return (
    <section>
      <header className="page-header">
        <h1>Jobs</h1>
        <Link className="button-link" to="/jobs/new">
          Submit a job
        </Link>
      </header>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <p className="muted">
        {data ? `${data.total} job${data.total === 1 ? "" : "s"}` : "Loading…"}
        {" · "}
        {active ? "refreshing every 2s" : "idle — checking every 15s"}
        {" · "}
        <button type="button" className="linkish" onClick={refreshNow}>
          Refresh now
        </button>
      </p>

      {data && jobs.length === 0 && <p>No jobs yet.</p>}

      {jobs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Status</th>
              <th>Priority</th>
              <th>Attempts</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  <Link to={`/jobs/${job.id}`}>
                    {JOB_TYPES[job.type]?.label ?? job.type}
                  </Link>
                </td>
                <td>
                  <StatusBadge status={job.status} />
                </td>
                <td>{job.priority}</td>
                <td>{job.attempt_count}</td>
                <td>{new Date(job.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data && data.total_pages > 1 && (
        <p className="pager">
          <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <span className="muted">
            Page {data.page} of {data.total_pages}
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
