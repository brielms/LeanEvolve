# Security

## Mathematical trust

Candidate programs are untrusted. A declaration receives fitness only after Lean
elaborates the candidate and reports an axiom dependency set allowed by the active
configuration. Textual source checks are an additional guard, not a replacement for
kernel verification.

The formal statement is an input. LeanEvolve can verify the supplied statement; it
cannot establish that the statement matches an informal intention. Keep that
specification boundary small and review it separately.

## Host security

Lean elaboration supports metaprogramming and can execute code at compile time.
Timeouts, a reduced environment, and source checks do not form a complete host
sandbox. Run model-authored candidates inside a disposable container or virtual
machine with:

- no production credentials;
- read-only source inputs;
- a dedicated writable results volume;
- network disabled unless the model provider requires it;
- CPU, memory, process, and storage limits.

The optional `sandbox_prefix` configuration field can prepend a site-specific
sandbox launcher to Lean commands. Its strength is entirely determined by that
external launcher and is recorded in evaluation receipts.

## Reporting

Report vulnerabilities privately through the repository's GitHub security advisory
workflow. Do not include secrets or sensitive run transcripts in public issues.
