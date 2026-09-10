# Full Stack PM Pilot for Codex

An experimental Codex plugin for learning modules and installing reusable skills in work projects.

**This release contains sample lessons only.** It supports anonymous module downloads, local progress, and a sample work skill. Real courses, account sign-in, cloud progress, and paid access are in development. It does not replace or change the existing Full Stack PM CLI.

## Install on a Mac

Requirements: an Apple Silicon Mac, Codex desktop with plugin support, and internet access. The plugin includes its own runtime; no Python, Node, pip, uv, or separate Full Stack PM CLI installation is required. Intel Macs, Windows, and Linux are not supported by this package.

The included executable has ad-hoc signatures, not Developer ID signing or notarization. Installation through the public Git marketplace is tested on an existing Mac; acceptance on a pristine Mac remains outstanding. If macOS blocks execution, stop and report the error; do not disable system protections.

Paste this into Terminal. It finds the Codex CLI bundled with the desktop app, registers this public marketplace, and installs the plugin:

```sh
fspm_codex_bin=""
for app in /Applications/Codex.app /Applications/ChatGPT.app; do
  if [ -x "$app/Contents/Resources/codex" ]; then
    fspm_codex_bin="$app/Contents/Resources/codex"
    break
  fi
done
if [ -n "$fspm_codex_bin" ]; then
  "$fspm_codex_bin" plugin marketplace add https://github.com/carlvellotti/fullstackpm-codex-pilot &&
  "$fspm_codex_bin" plugin add fullstackpm-pilot@fullstackpm-pilot
else
  echo "Codex was not found in Applications. Locate the app before continuing."
fi
```

If you already have the older Full Stack PM Pilot from `personal` or `fullstackpm-local-test`, disable that older copy in Codex Plugins to avoid duplicate tools.

Open an empty local folder in Codex, start a fresh task, and say:

> Use Full Stack PM Pilot to install the welcome fixture here and start the lesson.

Approve normal tool requests. No Full Stack PM login is required. Use local execution on the Mac where you want the files; a task running on a remote computer installs files on that remote computer.

## Try continuation and a work skill

Answer the lesson's product question and complete its exercise. In a fresh task in the same project, ask to continue with Full Stack PM Pilot. Your local checkpoint and practice files should remain available.

In a separate test work project, say:

> Install the Full Stack PM planning fixture skill in this project.

Only the namespaced skill is installed in `.agents/skills/fspm-pilot-planning-fixture/`. In a new task in that project, ask for help framing a product decision.

## Update or remove

Use Codex's plugin management to refresh this marketplace and reinstall the plugin, then start a fresh task. Downloaded lessons are pinned separately; updating the plugin does not overwrite student notes.

To uninstall, remove **Full Stack PM Pilot** in Codex Plugins. Downloaded lessons, practice files, and installed workplace skills remain in their projects. To deactivate an installed workplace skill while preserving its files, ask the pilot to remove that skill before uninstalling the plugin. The skill is archived locally and can be restored. You can remove a test folder yourself after preserving any work you want to keep.

## Troubleshooting

- Plugin missing: quit and reopen Codex, then start a new task. Check that `fullstackpm-pilot` is registered and the plugin is enabled.
- Wrong computer: use a local task rather than a remote host.
- Learning folder rejected: let the agent select or create a dedicated empty course folder. Existing work is preserved.
- Interrupted install: ask the agent to resume the same module. Keep staging files; do not clear them manually.
- Edited-file conflict: the installer preserves the files and stops. Ask for an explanation; cancellation requires an explicit request and preserves staging.
- Sign-in requested for a free lesson: report it. This release does not require Full Stack PM authentication.

Include the plugin version from `BUILD.json`, macOS/Codex version, tool error code, and steps to reproduce in a [GitHub issue](https://github.com/carlvellotti/fullstackpm-codex-pilot/issues). Do not post credentials, download grants, private workplace files, or personal course notes.

## Data and current limitations

The local helper handles downloads and file validation. Names, notes, local paths, and checkpoints remain on the computer. The hosted service receives content identifiers and random operation/workspace identifiers for installation. Ordinary hosting access logs may include IP addresses and request metadata.

This is a public Git marketplace distribution, not an approved listing in the Codex public plugin directory. Public-directory submission, real content, browser authentication, account sync, and paid access remain separate release gates. This version supports Apple Silicon Macs only.

The bundled runtime includes third-party license notices under `plugins/fullstackpm-pilot/runtime/Darwin-arm64/THIRD-PARTY-NOTICES`. `RUNTIME.json` records runtime files, executable flags, relative symlinks, and build input hashes; `BUILD.json` pins its hash and the implementation revision. `SHA256SUMS.json` records release-file hashes (and symlink targets). These checks detect corruption; they are not an independent publisher signature.
