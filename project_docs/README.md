# Project Documentation & Recovery Ledger

## Purpose
This directory tracks the active state, architectural decisions, implementation history, and recovery checkpoints for the **Mail AI Automation** platform.

If an outage, regression, or failure occurs, this ledger serves as the audit trail to reconstruct and recover system state.

---

## File Structure

| File | Purpose | Update Frequency |
|---|---|---|
| [`CURRENT_STATE.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/CURRENT_STATE.md) | High-level system snapshot, active services, open bugs, and verified capabilities. | Updated upon every component verification or schema change. |
| [`ARCHITECTURE_AUDIT.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/ARCHITECTURE_AUDIT.md) | Baseline gap analysis comparing user requirements against code implementation. | Updated when architectural decisions or threat models evolve. |
| [`ROADMAP_AND_MILESTONES.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/ROADMAP_AND_MILESTONES.md) | Sequenced phases, planned technical changes, and success criteria. | Updated as milestones start, progress, and complete. |
| [`TASK_LOG.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/TASK_LOG.md) | Detailed chronological record of all actions, code edits, and verification runs. | Updated at every execution step. |

---

## Recovery Protocol
If an unhandled exception or breaking change occurs:
1. Inspect [`CURRENT_STATE.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/CURRENT_STATE.md) to check the last known stable baseline.
2. Review the latest entries in [`TASK_LOG.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/TASK_LOG.md) to isolate the exact commit or file modification that introduced the defect.
3. Check [`ARCHITECTURE_AUDIT.md`](file:///home/hyper_is_op/mail_ai_automation/project_docs/ARCHITECTURE_AUDIT.md) to verify whether the failure violated core tenant isolation or state machine invariants.
