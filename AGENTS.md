# AGENTS.md

Instructions for any agent (human or LLM) working on this repository.

## HARD RULE — review findings are fixed in the PR, never filed for later

Binding on every agent (Kai, Lena, Omar, Robbie, Atlas, Max, Jake, any other) on every PR. Only the human principal can grant an exception, in writing, on the PR. This rule beats any older habit, template or procedure that says otherwise.

1. **Every review finding gets exactly one of two outcomes.** It's either **blocking**, so it gets fixed in this PR before approval, or it's **dropped**, because it isn't worth fixing. Say that in one line or don't mention it. There is no third outcome. "Non-blocking, follow-up", "filed as #NNN", "parked for a later PR" and "tracked separately" are all forbidden.
2. **Agents never open an issue without the principal's explicit OK.** This covers reviews, PR descriptions, ticks, pollers and triage. If you find a real bug in existing code outside the PR's scope, ask the principal in chat first, and only open the issue if they say yes.
3. **Follow-up PRs can't spawn more follow-ups.** Their findings get fixed in them or dropped.
4. **Nits are never blockers and never issues.** That covers style, naming, wording, comment polish and "could be cleaner". Drop them.
5. **Don't widen scope during review.** A PR fixes what it says it fixes. A finding that would need a separate change is either blocking, so it gets fixed here, or dropped.
6. **Only approve when nothing is left open.** An approval that lists leftovers is not an approval.

Why: review leftovers filed as issues turned every feature into a queue of follow-ups. Each one then needed its own PR and review, which filed more follow-ups. Fix it once, in the PR, or let it go.
