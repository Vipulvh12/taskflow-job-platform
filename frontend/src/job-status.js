/**
 * Job-status predicates. Plain JS, deliberately separate from StatusBadge.jsx:
 * deciding whether to keep polling is logic, not presentation, and keeping it
 * out of a component file means it can be imported and tested on its own.
 */

/**
 * Statuses a job can never leave.
 *
 * FAILED is included even though it is not a "ran and finished" state — it
 * means the queue publish failed at submission, so no worker ever saw the job
 * and nothing will ever move it. For polling purposes that is just as final as
 * SUCCESS or DEAD.
 */
export const TERMINAL_STATUSES = ["SUCCESS", "DEAD", "FAILED"];

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status);
}

/** True while anything in the list is still moving. Drives polling. */
export function hasActiveJobs(jobs) {
  return jobs.some((job) => !isTerminal(job.status));
}
