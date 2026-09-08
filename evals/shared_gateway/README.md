# Shared gateway acceptance coverage

`cases.json` preserves all 47 named scenarios from sections 7.1–7.4 of the unified gateway plan. These are requirements, not recorded passes. Native deployment/topology and performance requirements in section 7.5 remain additional gates; this manifest does not claim to enumerate their combinations.

The implemented checker rejects missing/extra/duplicate scenarios, non-passing outcomes, missing assertions/evidence, wrong OS/SHA, non-production dispatch, and leaked owned processes. It checks evidence availability, not whether a trace proves its claimed observation. Human/independent trace review remains mandatory. A loopback model is not live-provider evidence.

Run against a real driver's report:

    python3 evals/shared_gateway/verify_receipt.py receipt.json --expected-sha FULL_TESTED_SHA --expected-os linux

Evidence paths are relative to the receipt directory. Supply a full report with schema=1, source_sha, native_os, fixture_home_isolated=true, production_dispatch=true, model_boundary (loopback or live-provider), remaining_owned_processes=[], and cases. Each case contains name, status, positive integer assertions, and nonempty evidence_paths pointing to nonempty files.

## Retained standalone reader A/B

`search_readonly_ab.py` runs the real registered session-search tool in fresh interpreters with disposable homes. Its three cases cover empty, foreign-profile and existing-profile reads on both trees. It asserts the baseline creates an unwanted current-profile DB or commits a read-side write, while the fixed tree preserves marker reads without those mutations. Run with the project venv Python and explicit `--baseline`, `--fixed`, and private `--output` paths. The script records both tested SHAs and search-source hashes. Its receipt is deliberately narrower than the full `readonly-client` scenario, which also requires list/export and other reader paths.

The unified `run.py` driver is not implemented yet. Existing private native and subprocess probes have different receipt formats; do not rename them or manufacture a full report to obtain a pass. Test fixtures in `test_shared_gateway_harness.py` exercise the checker only and are not acceptance evidence.
