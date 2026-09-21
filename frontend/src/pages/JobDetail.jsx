import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { getJob } from "../api/jobs";
import { StatusBadge } from "../components/StatusBadge";
import { isTerminal } from "../job-status";
import { JOB_TYPES } from "../job-types";

const POLL_INTERVAL_MS = 2000;

function fmt(ts) {
  return ts ? new Date(ts).toLocaleTimeString() : "—";
}

export function JobDetail() {
  const { id } = useParams();
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  const fetchJob = useCallback(async () => {
    try {
      setJob(await getJob(id));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load job.");
    }
  }, [id]);

  useEffect(() => {
    fetchJob();
  }, [fetchJob]);

  const polling = job != null && !isTerminal(job.status);

  useEffect(() => {
    if (!polling) return undefined;
    const timer = setInterval(fetchJob, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [polling, fetchJob]);

  if (error) {
    return (
      <section>
        <p role="alert" className="error">
          {error}
        </p>
        <Link to="/jobs">Back to jobs</Link>
      </section>
    );
  }
  if (!job) return <p>Loading…</p>;

  const attempts = job.attempts ?? [];

  return (
    <section>
      <header className="page-header">
        <h1>{JOB_TYPES[job.type]?.label ?? job.type}</h1>
        <Link to="/jobs">Back to jobs</Link>
      </header>

      <p>
        <StatusBadge status={job.status} /> · priority {job.priority} · attempt{" "}
        {job.attempt_count} of {Math.max(job.attempt_count, attempts.length) || 0}
        {polling && <span className="muted"> · watching for updates…</span>}
      </p>
      <p className="muted mono">{job.id}</p>

      <h2>Payload</h2>
      <pre>{JSON.stringify(job.payload, null, 2)}</pre>

      {job.result != null && (
        <>
          <h2>Result</h2>
          <pre>{JSON.stringify(job.result, null, 2)}</pre>
        </>
      )}

      <h2>Attempt history</h2>
      {attempts.length === 0 ? (
        <p className="muted">
          No attempts recorded yet
          {job.status === "FAILED"
            ? " — this job was never queued, so no worker ever ran it."
            : "."}
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Outcome</th>
              <th>Started</th>
              <th>Finished</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {attempts.map((a) => (
              <Fragment key={a.id ?? a.attempt_number}>
                {job.attempt_base > 0 && a.attempt_number === job.attempt_base + 1 && (
                  <tr className="divider-row">
                    <td colSpan={5}>
                      Retried by an admin — a fresh budget of attempts starts here
                    </td>
                  </tr>
                )}
              <tr>
                <td>{a.attempt_number}</td>
                <td>
                  <StatusBadge status={a.status} />
                </td>
                <td>{fmt(a.started_at)}</td>
                <td>{fmt(a.completed_at)}</td>
                <td className="error-cell">{a.error ?? "—"}</td>
              </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
