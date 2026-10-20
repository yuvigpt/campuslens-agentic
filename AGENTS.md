# CampusLens Agentic: agent contract

The orchestration flow is implemented in `backend/app/agentic.py`. Agents emit
structured observations; they do not mutate status, publish reports, or choose
the final numeric priority directly.

| Agent | Purpose | Inputs | Output | Parallel? | Failure behavior |
|---|---|---|---|---|---|
| Understanding | Classify the issue without inventing missing facts | text, location, image availability | category, summary, entities, evidence, confidence, uncertainty | Yes | `Other`, low confidence, review flag |
| Safety | Identify evidence-backed hazards | text, understanding | safety level, hazards, evidence | Yes | low-confidence observation, review flag |
| Impact | Estimate qualitative population/facility impact | text, location, understanding | impact level, uncertainty | Yes | conservative estimate, no fabricated count |
| Urgency | Estimate response urgency | text, safety | urgency level, evidence | Yes | conservative fallback and review |
| Duplicate | Find likely same-location reports | text, location, candidate rows | group, relation, count, similar IDs | Yes | no merge when location/semantics do not support it |
| Routing | Map a known category to configured department | understanding, safety | department, confidence | After parallel stage | `Other` if unknown |
| Resolution | Suggest a practical next action | understanding, safety, routing | recommended action | After routing | generic human assessment action |
| Critic | Check contradictions and unsupported claims | all prior observations | PASS/WARNING/HUMAN_REVIEW_REQUIRED | After dependent stage | review required |
| Final decision | Assemble the structured result | all observations | final analysis + uncertainty | After critic | partial result with review required |
| Priority engine | Convert observations to score | safety, impact, urgency, category band, duplicate count | score, band, breakdown | After final decision | validates inputs; never guesses |

## Execution model

Understanding, Safety, Impact, Urgency, and Duplicate run concurrently in a
bounded thread pool. Routing and Resolution depend on that stage. Critic runs
after the dependent stage, then the deterministic priority engine is applied.
The current HTTP submission keeps the existing persistence-first behavior: the
complaint is saved before optional provider/orchestration enrichment.

## Human review

Review is recommended for low confidence, unknown category, contradictions,
image/text conflicts, or any partial agent failure. A recommendation is not an
action completed: only an authenticated admin status/action writes completion.
Future durable review records should remain separate from the advisory `ai_*`
columns and preserve the existing RLS and audit-event boundaries.

## Failure and privacy rules

- Provider failure never deletes a complaint.
- Agent failure is represented as a failed observation and does not crash the
  orchestration result.
- Exact affected-person counts are not fabricated.
- API keys, tokens, service-role keys, and private storage credentials never
  enter agent outputs or logs.
- `backend/app/scoring.py` is the only authority for the agentic numeric score.
