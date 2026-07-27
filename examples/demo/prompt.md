# Task

Improve the Lean candidate by closing configured declarations. Preserve every
kernel-accepted theorem already present. Work inside the evolution markers and make
the smallest useful proof-producing change.

The evaluator compiles the complete file and independently audits the axiom
dependencies of each configured declaration. Do not use placeholders, introduce
assumptions, or optimize for prose. If the whole frontier is too large for one turn,
choose one reachable formal subgoal.
