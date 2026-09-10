# Full Stack PM Codex pilot — real curriculum candidate

An invited test release for Apple Silicon Macs with Codex desktop. The plugin includes its local MCP installer and Python runtime and connects to a separate pilot staging service. The original Full Stack PM CLI is unchanged.

Foundation, PM Workflows, Vibe Coding and the Write Proposal work skill are available for free testing. Browser sign-in, paid Core access and account progress are undergoing acceptance. This candidate is not the final release.

## Install

Paste into Terminal on your Mac. This registers the candidate branch and installs the plugin:

```sh
fspm_codex_bin=""
for app in /Applications/Codex.app /Applications/ChatGPT.app; do
  if [ -x "$app/Contents/Resources/codex" ]; then
    fspm_codex_bin="$app/Contents/Resources/codex"
    break
  fi
done
if [ -n "$fspm_codex_bin" ]; then
  "$fspm_codex_bin" plugin marketplace add https://github.com/carlvellotti/fullstackpm-codex-pilot --ref codex/account-rc-20260910 &&
  "$fspm_codex_bin" plugin add fullstackpm-pilot@fullstackpm-pilot
else
  echo "Codex was not found in Applications. Locate the app before continuing."
fi
```

There is no separate Python or FSPM CLI installation. If another pilot marketplace is already configured, record its source/version before changing it. Disable duplicate pilot plugins so tools are unambiguous. Report registration conflicts instead of deleting configuration or course files.

Open a new empty local folder in Codex, start a fresh task and say:

> Use Full Stack PM Pilot. Install the Foundation module here and start my first lesson.

Use a local task on the Mac where you want the files. Free learning needs no Full Stack PM account. Your name is optional. Modules download individually with their practice dependencies; installation does not mean lesson completion.

After doing some lesson work, start a new task in the same folder and say:

> Continue my Full Stack PM lesson from my saved checkpoint.

## Apply a skill at work

Open a separate work project and say:

> Install the Full Stack PM Write Proposal skill in this project.

Only the namespaced skill and its supporting files are installed. Start a fresh task to use it and ask for help drafting a proposal.

## Optional accounts — acceptance in progress

Use Codex's authentication control for `fspm-pilot-account`. The plugin supplies the client, callback and requested scopes. Complete the Full Stack PM browser sign-in and consent flow. Never paste tokens into a task.

Connecting does not import guest history automatically. Ask explicitly to enable tracking or import existing progress. Account progress is a summary; exercise files and checkpoint prose are not synchronized between computers. Paid access requires a real membership. This candidate does not claim successful paid/account acceptance; those flows are part of the coordinated pilot check.

## Update, remove and recover

Refresh the registered marketplace and reinstall through Codex's plugin controls, then start a fresh task. Lesson releases are pinned separately, and updates preserve student files. Previous releases remain available for operator rollback.

Removing the plugin leaves lessons, practice files, checkpoints and workplace skills in their projects. To deactivate a work skill while keeping its files, ask the pilot to remove it before uninstalling the plugin. The skill is archived reversibly.

For interrupted downloads, ask to resume the same module and keep pending files. Edited-file conflicts are preserved for review; explicit cancellation archives the pending operation. Use a dedicated empty learning folder if the selected folder is rejected. Report runtime startup errors with the plugin version.

## Data and support

Local names, paths, exercise files and checkpoint prose stay local. The hosted service receives content IDs and random installation identifiers. Consented tracking uploads lesson/release/workspace IDs, attempt states and event times. Saving an account-profile name requires a separate request. Hosting logs may include IP addresses and request metadata. This pilot does not issue certificates.

Report issues at https://github.com/carlvellotti/fullstackpm-codex-pilot/issues with the plugin version, macOS/Codex version, error code and reproduction steps. Keep credentials, private course content and workplace files out of public reports.

This is a public Git marketplace prerelease, not an approved Codex directory listing. Only Apple Silicon Mac is a supported student target. See `ACCEPTANCE.md` for coverage. Runtime notices are included in the plugin; `BUILD.json` records provenance and `SHA256SUMS.json` records file hashes and symlink targets.
