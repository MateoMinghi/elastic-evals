# Research: self-improving agent harnesses driven by evals

Date of research: 2026-10-22. Question: can the harnesses evaluated by elastic-evals improve themselves against its suites, and what should such a loop take from existing work? Method: a full read of metaevo-ai/harness-zero (code and skill files), and abstracts, papers and repositories for the rest. Items marked **[UNVERIFIED]** were seen only in summaries.

The *harness* is everything around the model: system prompt, tool definitions and descriptions, skills, memory, middleware (guards, retries, step budgets), and configuration. *Harness optimisation* edits that layer, not the model weights, and keeps an edit only if evals show it helps. When the thing that proposes edits is itself an agent whose harness can be improved the same way, the loop is recursive: this is what "recursive self-improvement" (RSI) means in the papers below.

## 1. harness-zero (metaevo-ai)

[github.com/metaevo-ai/harness-zero](https://github.com/metaevo-ai/harness-zero), Apache-2.0. **Its main contribution is harness *distillation*, not optimisation.** The Python code runs a teacher/student review loop and SFT. The evolution step itself is not code: it is a workflow for a coding agent, written as a skill (`.agents/skills/student-harness-evolve/SKILL.md`).

| Aspect | harness-zero |
|---|---|
| What is edited | A per-domain "bank": `tools/*.py` (`make_tool`), `middlewares/*.py` (guards: output, turn budget, retry loop, submit gate), `skills/*/SKILL.md`, `memory.md`, a `manifest.json` that switches components on and off, and a `registry.py`. The bash tool, base prompt, turn limit and review middleware are frozen |
| Who proposes | A main coding agent plus up to 10 analysis subagents. The subagents only write notes; the main agent merges them and edits the bank |
| Fitness | The benchmark verifier's reward, from rollouts in Docker sandboxes |
| Selection | Greedy, single lineage, 3 rounds. Each round re-runs only the tasks still failing. Solved tasks leave the campaign, so **nothing checks that a change did not break a task solved earlier**. A component with no benefit for 2 rounds is switched off |
| Feedback | Full traces: trajectories, LLM calls, verifier output. Failures are sorted into classes and attributed with a checklist: did the bank load, was the new tool called, did the middleware fire, was it infrastructure or the turn limit |
| Safeguards | Rules in prose, not enforced in code: nothing task-specific in the bank, generalise before adding, every change must point to a trace, prune bloat. Disjoint train and test splits |
| Bank provenance | `load_student_harness()` validates every reference and hashes the bank (`bank_sha256`) |

The evolved harness is then turned into a private reference for a teacher. The teacher reviews the student's steps, and successful trials become SFT data, so a small model learns to behave as if the harness were attached (reported: 23.3% → 44.3% without the harness, against 41.7% with it).

## 2. Related work

| Work | Edits | Search | Feedback | Note |
|---|---|---|---|---|
| [Meta-Harness](https://arxiv.org/abs/2603.28052) (Stanford, 2026) | Harness code | Claude Code as proposer; every candidate kept; Pareto front tested at the end | The proposer browses a filesystem of all earlier candidates' code, scores and raw traces | Argues compressed feedback hurts. Search and test overlapped on one benchmark |
| [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954) (Sakana) | The agent's own Python | Open-ended archive; weak "stepping stones" stay eligible as parents | Benchmark score | **Documented objective hacking**: the agent removed hallucination markers and faked test logs. The lineage log is how it was caught |
| [GEPA](https://arxiv.org/abs/2507.19457) (`dspy.GEPA`) | Prompts | Per-instance Pareto front of candidates; minibatch check before full evaluation | Natural-language reflection on trajectories plus textual feedback from the metric | Reported to beat GRPO with far fewer rollouts |
| [ADAS / Meta Agent Search](https://arxiv.org/abs/2408.08435) | Whole agents as code | Archive conditions the next proposal | Scores | Fixed meta-agent |
| [SICA](https://arxiv.org/abs/2504.15228) | Its own codebase | Best agent in the archive improves itself | Score, cost and time | An asynchronous LLM overseer watches runs |
| [AlphaEvolve](https://arxiv.org/abs/2506.13131) / [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) | Marked code blocks, as diffs | MAP-Elites, islands | Evaluator output ("artifacts") fed back | Cascaded evaluation: cheap stages first |
| [MIPROv2](https://arxiv.org/abs/2406.11695), [TextGrad](https://arxiv.org/abs/2406.07496), [Promptbreeder](https://arxiv.org/abs/2309.16797) | Prompts, demos | Bayesian optimisation; textual gradients; GA that also evolves its mutation prompts | Scores; critiques | Promptbreeder is the earliest "the optimiser improves itself" |
| [Better-Harness](https://www.langchain.com/blog/better-harness-a-recipe-for-harness-hill-climbing-with-evals) (LangChain) | Harness | Hill climbing | Evals | Optimisation and holdout splits per eval category; cases that pass become regression tests |

Newer 2026 papers on harness search (HarnessBank, Recursive Harness Self-Improvement and others) were seen only in summaries **[UNVERIFIED]**. One idea from the HarnessBank summary is worth noting: a candidate must pass validity, *activation* (the change actually fires), a paired significance test, and beat its parent.

## 3. What the field agrees on

1. **Freeze a kernel, edit a surface.** The evaluator, the data, the interfaces and the loop are out of reach of the proposer. Only a declared set of files changes.
2. **Feed back traces, not just scores.** Every recent system gives the proposer the failing transcripts and why they failed.
3. **Keep an archive with lineage.** Keep more than the single best candidate (Pareto front, MAP-Elites, open-ended archive), and log every attempt. Without the log, hacking goes unnoticed.
4. **Separate search from selection from reporting.** Use one split to propose, one to accept and a held-out split scored once. Regression gates on earlier successes are often missing (harness-zero) and are cheap to add.
5. **Expect the optimiser to exploit the evaluator.** DGM is the standard example. The evaluator has to be outside the surface, and the proposer should not see how it decides.

## 4. Fit with elastic-evals

elastic-evals already has most of the kernel such a loop needs. It also has something benchmark-driven loops lack: **an evaluator whose accuracy is measured**, so the risk of optimising against a fallible judge can be checked rather than assumed away.

| Loop needs | Already here |
|---|---|
| Run a harness version over items and score it | `experiments.run` with the production suite and calibration |
| Accept or reject | Experiment gates: absolute on critical checks, relative to a baseline on the rest, promptfoo-style assertions |
| Items | Datasets built from production conversations (bulk, "similar failures" by k-NN), and `synth` for cold start |
| Feedback for the proposer | Per-check decisions with the check's `ask`, the transcript, the record, failed assertions |
| An independent check on the evaluator | The audit judge (a different model family from the routing judge) and human labels |
| Ship safely | Experiments stored next to production scores; `trace_ctx.release` separates versions in production |

**Goodhart is sharper here than on a benchmark.** A verifier on SWE-bench is (nearly) exact. A calibrated cascade is about 93% accurate (on the synthetic holdout). An optimiser that pushes hard enough will find the 7%, for example by phrasing that lexical rules or a small head misread. Defences, in order of strength:

- **Critical checks are anchored in tier 0** (record comparisons): a model may add a flag, never clear one, so the most important checks cannot be argued away by wording.
- **The proposer sees each check's `ask`, never its rule, route, thresholds or judge prompt.**
- **On the held-out split, the audit judge re-scores every item** (suites that allow hosted evaluators). If the audit judge's agreement with the routes falls on the evolved harness while the routed score rises, the gain is suspect.
- **Humans close the loop.** The suspect items go to an annotation queue, and labels from the new harness's traffic feed the next calibration. The evaluator then learns the new failure modes instead of being frozen against them.

## 5. What was built

`elastic_evals/evolve.py` and `python -m elastic_evals evolve` ([DESIGN.md §6.11](../DESIGN.md#611-harness-optimisation-evolve)). It is a GEPA-style loop over a harness directory, with the kernel/surface split, trace-level feedback, per-item Pareto parent selection, minibatch then validation acceptance with paired and regression gates, a lineage archive, and a held-out test split audited by the independent judge. The proposer is pluggable: an LLM by default, or any function, including a coding agent.

## 6. What is not built yet

| Step | Why it matters | Sketch |
|---|---|---|
| **The online loop** | Offline items go stale; production finds the new failures | Flagged production conversations → dataset items → `evolve` → candidate run as an experiment, then canaried under its own `trace_ctx.release` → compare with production scores → promote by hand |
| **Proposer self-improvement** (the recursive part) | Promptbreeder and DGM improve the improver | Treat the proposer's prompt as a harness. Its fitness is the validation gain its proposals achieve across many evolve runs. Needs many runs to be measurable |
| **Code in the surface** | Tools and middleware are where harness-zero found most of its gains | Allow `*.py` only with the agent running in a sandbox (container per evaluation); the loop must not import proposer-written code in its own process |
| **Activation checks** | A change that never fires cannot be the cause of a gain | Trajectory checks on spans tagged by the harness component that produced them |
| **MCP and API** | Let an operator's agent run the loop on a deployment | `evolve` over a stored dataset, candidates stored as experiment runs |
| **Cost as an objective** | A better harness that doubles tokens may not be better | Agent cost from spans (already priced at ingest) as a second Pareto axis |
