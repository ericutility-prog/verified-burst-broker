# verified-burst — independent judge proof  (2026-06-24T02:31:38Z)

**120** programmatically-generated, mechanically-checkable decisions. Generator **gpt-oss-120b**; independent judge **zai-glm-4.7** (a different model family). Ground truth computed in code (seed 1729, reproducible). Items, generator, and judge are the product's real path — nothing hand-picked.

- Base model got **104/120** right (86.7%); **16** wrong.
- Of those 16 mistakes, the independent judge **caught 16** (**100.0%**) — agent told to HOLD, **free** (a miss isn't charged).
- It waved **0** wrong answers through (**false-confirm 0.0%** — the case we're NOT hiding: you'd pay and act on a bug).
- On correct answers it false-alarmed **4/104** (**3.8%**) — a wasted redo, but free.
- **When you ARE charged, the answer was right 100.0% of the time** (precision over the 100 charged decisions).

**Economics:** fee $0.0045 per charged (judge-passed) decision. This run charged $0.45 total and delivered **16 caught mistakes for free** — downside on a catch is **$0** by construction (a miss never settles).

_Reproduce: `python proof_harness.py 120` — same seed, same items._
