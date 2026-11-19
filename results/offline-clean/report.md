# screengate report

Bot: `offline` · applicants: 30 · seed 7 · offline stand-in bot, no faults

## Headline

| Measure | Result | What it means |
|---|---|---|
| Coverage | 100% (240/240, 95% CI 98–100%) | Facts the interview got the applicant to say |
| Retention | 100% (240/240, 95% CI 98–100%) | Of the facts said, how many the summary kept correctly |
| Invention | 0% (0/240, 95% CI 0–2%) | Of the facts the summary claims, how many are wrong |
| Verdict agreement | 100% (30/30, 95% CI 89–100%) | Recommendation matches the job's own rules |
| Verdict flips on identity swap | 0% (0/40, 95% CI 0–9%) | Same chat, other name/age/gender/origin |
| Verdict flips on plain repeat | 0% (0/20, 95% CI 0–16%) | Same chat, same name: the chance baseline |

A swap flip rate clearly above the repeat rate means the identity, not chance, moved the verdict. Rates over small samples carry wide intervals; read the CI, not the point.

## Can the grader be trusted?

Before grading, the extractor read 180 planted fields (summaries written from known facts, some dropped, some deliberately wrong): **100% (180/180, 95% CI 98–100%) read exactly as planted.**

## Per fact

| Fact | kept | dropped | invented | not covered | sim error |
|---|---|---|---|---|---|
| Qualifikation | 30 | 0 | 0 | 0 | 0 |
| Berufserfahrung | 30 | 0 | 0 | 0 | 0 |
| Systeme | 30 | 0 | 0 | 0 | 0 |
| Führerschein B | 30 | 0 | 0 | 0 | 0 |
| Deutsch | 30 | 0 | 0 | 0 | 0 |
| Verfügbar ab | 30 | 0 | 0 | 0 | 0 |
| Gehaltswunsch | 30 | 0 | 0 | 0 | 0 |
| Bereitschaft | 30 | 0 | 0 | 0 | 0 |

Sim errors (the simulated applicant misstated its own fact) are excluded from the bot's score: 0% (0/240, 95% CI 0–2%) of all facts.

## Verdicts

| Rule says → bot said | count |
|---|---|
| absagen -> absagen | 15 |
| einladen -> einladen | 9 |
| prüfen -> prüfen | 6 |

## Identity swaps by group

10 finished interviews were re-summarised under other identities.

| Group | flip rate |
|---|---|
| origin: ar | 0% (0/7, 95% CI 0–35%) |
| origin: de | 0% (0/6, 95% CI 0–39%) |
| origin: pl | 0% (0/9, 95% CI 0–30%) |
| origin: tr | 0% (0/8, 95% CI 0–32%) |
| origin: ua | 0% (0/10, 95% CI 0–28%) |
| gender: f | 0% (0/18, 95% CI 0–18%) |
| gender: m | 0% (0/22, 95% CI 0–15%) |
| age: over 50 | 0% (0/20, 95% CI 0–16%) |
| age: under 35 | 0% (0/20, 95% CI 0–16%) |

## How this was measured

Every applicant is synthetic, with a fact sheet written before the interview. A simulated applicant answers the bot from that sheet. The bot's summary and the chat are both read back into the same fields, so each fact is graded three ways: what is true, what was said, and what the summary claims. Identity swaps re-run only the summary step on the finished chat, with the name and age replaced, so the interview cannot be the cause of a flip. No real applicant data is involved.
