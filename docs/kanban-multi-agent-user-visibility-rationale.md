# Kanban multi-agent user visibility rationale

This note captures the operating decision for Slack/messaging-originated Kanban work: do not collapse back to a single all-powerful agent. Keep the multi-agent Kanban architecture, but make root-request status visible to the user.

## Decision

Hermes should keep the low-privilege gateway + Kanban-first + worker-profile split.

The user-facing experience must be aggregated at the root request/thread level. Child cards, worker retries, blockers, and dependent TODOs are internal implementation details until they affect the user; when they do, the gateway must explain them in the original Slack/Telegram thread.

## Why not a single all-powerful agent

A single agent with gateway access plus terminal/file/network privileges would be simpler to reason about in the short term, but it creates the wrong failure mode:

1. Remote-command blast radius grows too large.
   The gateway is exposed to messaging platforms and untrusted or semi-trusted chat input. Giving that same process broad local execution privileges turns every Slack conversation into a high-risk automation surface.

2. Long-running work loses audit structure.
   Kanban cards provide durable task state, dependencies, retries, comments, artifacts, assignees, and event history. A single chat-session agent can appear simpler while making it harder to reconstruct why a task is waiting or what changed.

3. Quality gates become optional.
   Separate PM/dev/research/review/safe profiles allow capability scoping and review stages. A single agent tends to combine specification, execution, and review in one authority path.

4. Least privilege becomes impossible to enforce cleanly.
   Some work only needs summarization or planning; some needs local files; some needs terminal; some should be read-only. Worker profiles make those differences explicit.

5. User experience problems are caused by missing aggregation, not by decomposition itself.
   The observed issue was that blocked child cards and queued dependent work were not explained in the root Slack thread. That is a notification/aggregation defect, not evidence that all tasks should run inside one agent.

## Required UX contract

For each Slack/messaging-originated root request:

1. Child events must surface in the original root thread when they affect progress.
2. A blocked message must distinguish:
   - user action needed: the user must answer or clarify something;
   - system/operator action needed: missing tools, permissions, profile mismatch, worker crash, retry limit;
   - external dependency: waiting on a third-party service or artifact.
3. Queued TODOs must be explained by dependency:
   - waiting for parent task;
   - runnable but not yet claimed;
   - blocked by system/operator issue;
   - blocked by user answer.
4. The message should name both the child card and the root/parent request, so the user does not need to inspect the board to understand why their request stopped.
5. Recovery should be explicit: reassign, update toolset/profile, retry, ask user, or cancel.

## Implementation guardrails

- Root notify subscriptions should propagate to child cards when decomposition links are created.
- Adding a notify subscription after decomposition should also cover existing descendants.
- Gateway blocked-event formatting should include parent/root context and action ownership.
- Notification delivery must remain gateway/notifier-driven; do not create a special child task whose only purpose is to notify users about other blocked child tasks.
- Capability checks should happen before dispatch where possible, so tasks that require file/terminal access are not routed to profiles without those tools.
- The gateway should stay low-privilege; recovery actions that require elevated access should be delegated to appropriate worker profiles or operator workflows.

## Current implemented baseline

The notifier now has regression coverage for:

- parent/root notification subscriptions propagating to a child when `link_tasks(parent, child)` is called;
- subscriptions added to a root after decomposition propagating to existing descendants;
- child blocked notifications naming the parent/root request and distinguishing user action from system/operator action for tool/profile blockers;
- deterministic root/request status snapshots exposed through `hermes kanban status <id>` and JSON output;
- rate-limited root queue digests that list queued child work, assignees, and parent chains even when no terminal event fired;
- digest retry safety: a failed Slack/Telegram send resets the digest claim so queued work is not hidden for the full rate-limit window;
- visible Slack ACKs for new requests and busy follow-up replies; and
- short Slack choice/checkpoint replies such as `AACC` being recorded as both a Kanban comment and a `user_feedback_received` event.

This baseline addresses the immediate silent-child-blocker / silent-queued-work failure while preserving the multi-agent architecture.
