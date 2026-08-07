# Issue tracker: Local Markdown

Issues and specs (you may know a spec as a PRD) for this repo live as markdown files in `.scratch/`.

Chosen over GitHub Issues deliberately: this repo is public, and work-in-progress specs and tickets should not appear on it by default. Publishing anything to GitHub is a separate, explicit decision each time.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` — never a single combined tickets file
- Triage state is recorded as a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.

## Relationship to `docs/` (this repo's long-lived documents)

`.scratch/` is the working tracker — in-flight specs and tickets. It is not the home for decisions that outlive the work:

| Document | Home | Lifetime |
| --- | --- | --- |
| In-flight spec / tickets | `.scratch/<feature>/` | until the work ships |
| Architecture decision | `<package>/docs/adr/` | permanent |
| Accepted formal spec | `<package>/docs/specs/` | permanent |
| File-level implementation plan | `<package>/docs/plans/` | until implemented |

A spec that has been accepted and is being implemented belongs in the package's `docs/specs/`, not in `.scratch/`.

## Is `.scratch/` committed?

Not decided yet. Nothing under `.scratch/` exists at the time of writing. Decide the first time a ticket is created, and record the answer here — an undecided `.gitignore` status is how scratch trackers quietly diverge between machines.
