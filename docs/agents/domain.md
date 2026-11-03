# Domain docs

## Layout

This is a single-context repository:
- `CONTEXT.md` at the repository root: domain vocabulary and context.
- `docs/adr/`: architecture decision records.

## Before exploring

Read `CONTEXT.md` and ADRs relevant to the area being explored.
If these files do not exist, proceed silently. Do not propose creating them
upfront: domain-modeling creates them lazily as terms and decisions are resolved.

If the repository later adopts `CONTEXT-MAP.md`, read it first and follow its
pointers to the relevant per-context `CONTEXT.md` files and scoped ADRs.

## Vocabulary

Use the glossary's terms in issue titles, proposals, hypotheses, and tests.
Avoid synonyms the glossary explicitly rejects. If a needed concept is absent,
reconsider whether it belongs or note the gap for domain-modeling.

## ADR conflicts

Explicitly flag any proposal that contradicts an existing ADR and explain why
the decision should be reopened rather than silently overriding it.
