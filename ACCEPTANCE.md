# Candidate acceptance — 2026-09-10

This is an integration candidate. Final pilot acceptance is incomplete.

| Check | Result |
|---|---|
| Full implementation check | 94 TypeScript and 129 Python tests, typecheck/build passed |
| Explicit-scope packaging correction | Focused plugin tests and typecheck passed |
| Bundled runtime | Same verified Apple Silicon Python 3.13.15 runtime as previous public release |
| Account-configured staging | Public tools return 200 with no auth challenge; account transport returns 401 with resource metadata |
| Mac installed plugin discovery | Four public and eight local tools available with account logged out; Codex 0.153.1 |
| Real curriculum on Mac | Three public modules, 212 file hashes, checkpoint/fresh-process resume and file preservation passed |
| Separate workplace skill | Write Proposal installation passed; existing workplace file preserved |
| Anonymous paid request | Core denied; no paid module/practice files installed |
| Browser authorization request | Corrected installed plugin sends two pilot scopes, one resource, registered client and S256; reaches Clerk sign-in host browser verification in Chromium |
| Completed browser login and token acceptance | Pending |
| Real membership and paid installation | Pending audited reader approval and actual account acceptance |
| Real account progress import/sync | Pending authenticated acceptance |
| Fresh public Git installation of this candidate | Pending |
| Final Codex desktop student walkthrough | Pending |

The service is the isolated staging deployment `dpl_3DnMakGuvGEuVSLvvFdPuoaMuT1q` at https://fullstackpm-mcp-pilot-staging.vercel.app. Its curriculum registry SHA-256 is `041642d8d727944ca48931886a6b11c06659a987ca8ba438e1a792403b89e869`. Private archives are not included in static public files or this plugin.

Tests described as command-line, app-server or transport checks do not establish the desktop UI experience. Local exercise files are not cloud-synchronized. The earlier fixture release remains available and is not silently replaced by this candidate branch.
