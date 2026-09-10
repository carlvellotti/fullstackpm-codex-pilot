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
| Completed browser login and token acceptance | Passed: real Chromium consent/Codex callback, exact audience, five account tools and profile read; cancel and host refresh also verified |
| Real membership and paid installation | Passed for the actual paid account: private Core practice and 10.6 MB course archive installed on Mac, 158 inventory files verified; nonmember test remains pending |
| Real account progress import/sync | Passed: explicit QA guest import, subsequent sync, second-client summary read; checkpoint text stays local |
| Installed specialist agent | Passed in a fresh task with normal persisted learning-project trust; actual child result inspected |
| Fresh public Git installation of this candidate | Passed on Mac Codex 0.153.1: correct version and all runtime files verified, four public/eight local tools, optional account logged out, removal/reinstall passed |
| Final Codex desktop student walkthrough | Pending |

Plugin `0.1.0+codex.20260910163721` adds trust/reload guidance to the unchanged verified runtime. Fresh public installation of this new version is being checked.

The service is the isolated staging deployment `dpl_4b86nKxDcHF6L6YHEeeoD4vFkWFZ` at https://fullstackpm-mcp-pilot-staging.vercel.app. Its curriculum registry SHA-256 is `041642d8d727944ca48931886a6b11c06659a987ca8ba438e1a792403b89e869`. Private archives are not included in static public files or this plugin.

Tests described as command-line, app-server or transport checks do not establish the desktop UI experience. Local exercise files are not cloud-synchronized. The earlier fixture release remains available and is not silently replaced by this candidate branch.
