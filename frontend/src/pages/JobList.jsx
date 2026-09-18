import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listJobs } from "../api/jobs";
import { StatusBadge } from "../components/StatusBadge";
import { hasActiveJobs } from "../job-status";
import { JOB_TYPES } from "../job-types";

const POLL_INTERVAL_MS = 2000;

export function JobList() {
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const fetchJobs = useCallback(async () => {
    try {
      setData(await listJobs({ page, pageSize: 20 }));
      setError(null);
    } catch {
      setError("Could not load jobs.");
    }
  }, [page]);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  const jobs = data?.items ?? [];
  const polling = hasActiveJobs(jobs);

  useEffect(() => {
    // Polling runs exactly while something on this page is still moving. A
    // finished page must not hit the API every 2s forever — and because this
    // is derived state rather than a one-way stop, polling resumes by itself
    // when a newly submitted job appears.
    if (!polling) return undefined;
    const id = setInterval(fetchJobs, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [polling, fetchJobs]);

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
        {polling ? "auto-refreshing every 2s" : "idle — nothing in progress"}
        {!polling && (
          <>
            {" · "}
            <button type="button" className="linkish" onClick={fetchJobs}>
              Refresh
            </button>
          </>
        )}
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
