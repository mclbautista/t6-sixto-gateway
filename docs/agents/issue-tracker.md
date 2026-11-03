# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `mclbautista/t6-sixto-gateway`.
Use the `gh` CLI from this clone; it infers the repository from the remote.

## Conventions

- Create: `gh issue create --title "..." --body-file <path>`
- Read a ticket and its discussion: `gh issue view <number> --comments`
- Read labels and metadata: `gh issue view <number> --json number,title,body,labels,assignees,state`
- List: `gh issue list --state open --json number,title,body,labels,assignees`
- Comment: `gh issue comment <number> --body-file <path>`
- Apply/remove labels: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and PR numbers. For an ambiguous reference, try
`gh pr view <number>` and fall back to `gh issue view <number>`.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, containing Notes, Decisions-so-far,
  and Fog.
- Children: issues labelled `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, or `wayfinder:task`. Link them as GitHub sub-issues.
  If unavailable, use a task list in the map and `Part of #<map>` in each child.
- Blocking: use native GitHub issue dependencies. Retrieve the blocker's
  database ID with `gh api repos/mclbautista/t6-sixto-gateway/issues/<number> --jq .id`.
  Add it with `gh api --method POST repos/mclbautista/t6-sixto-gateway/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`.
  If unavailable, record `Blocked by: #<number>` in the child body.
- Frontier: choose the first open, unassigned child in map order with no open
  blockers. Use `issue_dependencies_summary.blocked_by` for native dependencies;
  otherwise check the referenced blocker issues.
- Claim: `gh issue edit <number> --add-assignee @me`.
- Resolve: comment with the answer, close the child, and append a gist and link
  to the map's Decisions-so-far section.
