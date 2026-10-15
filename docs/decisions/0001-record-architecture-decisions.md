# 0001. Record architecture decisions

- Status: accepted
- Date: 2026-10-15

## Context

`docs/architecture.md` is the design and the source of truth. Over fourteen build steps some choices will
have to change. Without a record, later sessions and reviewers can't tell a deliberate change from drift.

## Decision

Every deviation from `docs/architecture.md`, and every significant choice it leaves open, gets a short
Architecture Decision Record in `docs/decisions/`, numbered `NNNN-kebab-case-title.md`. The same branch updates
`docs/architecture.md` to match. ADRs are never edited after acceptance; a later ADR supersedes an earlier one.

## Consequences

- The design doc stays accurate, and the reason behind each change is preserved.
- A small cost per decision: one file of a few paragraphs.

---

## Template

```markdown
# NNNN. Title

- Status: proposed | accepted | superseded by NNNN
- Date: YYYY-MM-DD

## Context
What forces the decision: the problem, constraints, what the architecture currently says.

## Decision
What we do, stated in full sentences.

## Consequences
What becomes easier or harder; what has to change elsewhere (code, tests, docs/architecture.md).
```
