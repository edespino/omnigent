---
name: local-review
description: On-demand variant of fanout that keeps each task's branch and diff LOCAL — no PR is opened — and cross-reviews the worktree diff with a different-vendor sub-agent. Delivers the local branch plus review report; polly never merges. Use when the human explicitly asks to review locally / skip the PR.
---

# local-review — parallel execution without a PR

On-demand toggle. Same isolation and cross-vendor verification as `fanout`, but
the deliverable is a LOCAL branch plus a review report — no PR is opened. Use
ONLY when the human explicitly asks to skip the PR (e.g. "review locally",
"no PR"); the default remains `fanout` (each implementer opens its own PR).
Subtasks must still be parallel-safe (no shared files, no ordering dependency).

This skill is defined as DELTAS from `fanout`. Follow `fanout` for every shared
convention — worktree creation and `.worktrees/<task_id>` + `polly/<task_id>`
naming, the `.polly/registry.json` entry, one implementer per worktree, the
co-sign trailer, recording each `conversation_id`, dispatching the whole
parallel-safe set in one turn then ending it, inbox collection, and failure
handling (cancel + re-dispatch, never re-prompt a dark worker). Only the points
below differ.

## Deltas from `fanout`
1. **Step 1 (worktree):** unchanged, plus mark the task `no-pr` in the registry
   so later steps neither open nor expect a PR.
2. **Step 2 (dispatch):** unchanged EXCEPT the worker commits to its worktree
   branch and stops there — it must NOT open a PR (no `gh pr create`) and must
   NOT push. State this explicitly in the task input. (Co-sign trailer,
   `conversation_id` recording, and same-turn dispatch all still apply.)
3. **Step 3 (collect):** unchanged EXCEPT there is no PR URL to record.
4. **Step 4 (review):** run `cross-review` against the LOCAL diff —
   `git -C .worktrees/<task_id> diff main...HEAD` (NOT `gh pr diff`; there is no
   PR). Cross-review is otherwise identical: a DIFFERENT-vendor reviewer, the
   deterministic gates first, blocking issues looped back to the SAME
   implementer conversation until clean.
5. **Step 5 (deliverable):** the artifact is the LOCAL branch plus the
   cross-review report. When gates are green and cross-review has zero blocking
   issues, mark the task ready in the registry with its branch (`polly/<task_id>`),
   its worktree path, and a short review summary. There is NO PR URL. polly does
   NOT merge and does NOT push — the human inspects the branch and decides next.
6. **Step 6 (cleanup) — INVERTED vs `fanout`:** do NOT remove the worktree.
   The branch lives ONLY on local disk (no remote, no PR), so `git worktree
   remove` here would DESTROY the work. Leave the worktree and branch in place;
   remove one only after the human confirms the work is no longer needed.

## Notes
- This is the on-demand toggle: it changes nothing about polly's default.
  Absent an explicit "local / no PR" request, use `fanout`.
- Everything `cross-review` requires still holds: a reviewer from a DIFFERENT
  vendor than the implementer, so at least two AVAILABLE workers (per polly's
  roster preflight). Give the reviewer ONLY the diff + contract — never the
  implementer's worktree or transcript.
- Preserve worktrees for the whole batch until the human collects the work. If
  local disk is a concern, hand the human a portable patch instead of deleting —
  `git -C .worktrees/<task_id> format-patch main --stdout > .polly/<task_id>.patch`
  — then the branch is reproducible with `git am` and the worktree is
  disposable.
- Drift watch: this skill restates only the `fanout` deltas above. If `fanout`
  changes its shared steps (worktree/branch naming, co-sign trailer, dispatch
  conventions) or `cross-review` changes its diff-fetch / reviewer contract /
  vendor-independence rule, re-check these deltas still line up.
