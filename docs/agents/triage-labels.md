# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## How labels are applied here

The tracker is local markdown (see `issue-tracker.md`), so a label is a `Status:` line near the top of the issue file, not a tracker-side object:

```markdown
Status: ready-for-agent
```

There is nothing to create ahead of time — no label needs to exist anywhere before it can be used.

## Note on GitHub

This repo's GitHub Issues carries only the nine GitHub default labels; none of the five above exist there. That is fine and expected — the local tracker is the source of truth. If work is ever promoted to a GitHub issue, the labels have to be created there first (`gh label create`), which is an outward-facing change to a public repo and should be confirmed before doing it.
