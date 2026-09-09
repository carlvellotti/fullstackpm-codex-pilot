---
name: fspm-pilot
description: Use the independent Full Stack PM Pilot to start or continue learning modules and install reusable work skills. This pilot uses anonymous synthetic fixtures and bundled local tools.
---

# Full Stack PM Pilot

Use the plugin's hosted tools for catalog discovery and its bundled local tools for installation and guest state. Tool names may carry a host prefix. Never install or invoke `fspm`, reconstruct Python from Markdown, or substitute shell installation scripts.

## Start or continue

1. Call `fspm_pilot_local_status`, with the exact selected absolute project root if known. Verify its execution host matches the intended project. The plugin process's working directory is not the project. If the project is missing or ambiguous, clarify its location. Inspect `learning_destination`: overall `status: ready` describes the tool, not permission to install into a nonempty project. If exactly one existing learning root is returned below the selected project, check that root and continue there. If several exist, ask which course folder to use. If none exists and the selected project is nonempty, create a new empty `fullstackpm-learning` subfolder using the agent's normal file tools, then check it; never repurpose or clear an existing directory. Keep all course modules in that same learning root, rather than creating a folder for each module.
2. In a learning folder with saved state, resume the installed lesson/checkpoint. Do not reinstall merely because a fresh task has begun. `workspace.active.checkpoint` is an installation marker; use `learning.attempts`/`lesson_state` for actual student progress. Names are offered by the learner; respect a recorded decline. Installation is not completion.
3. For available content, call `fspm_pilot_catalog` on the public connection. Explain once that these are synthetic pilot fixtures, not the real curriculum. Public use needs no Full Stack PM login. Do not probe or connect the optional account service during free use.
4. For a requested install, call `fspm_pilot_install` with the project root, `kind: learning` or `work`, and the catalog item ID. The local tool prepares the remote plan and performs deterministic installation in one call. Read [local tool guidance](references/native-install.md) only when recovery/state details are needed; it contains no executable recipe.
5. Read the returned entrypoint. Before teaching a new lesson, record `started` with `fspm_pilot_checkpoint`: create stable attempt/event UUIDs and use `expected_revision: 0`. Continue using that attempt ID and the returned revision, with a fresh event ID for each later event. Reuse an existing incomplete attempt when resuming. A learner-requested repeat uses a new attempt; never regress the old one. Do not record lesson checkpoints for work skills.
6. Teach the lesson and stop at student participation points. Collect each required answer before moving on. For the welcome fixture, collect the actual product question as well as the requested note; a learning goal alone is not the product question. Save the question in a concise local checkpoint before asking for the note, so a fresh task can continue. If a learner says “done,” inspect the work and check all lesson requirements before recording completion. Ask for any missing required answer instead of completing prematurely. Keep user-facing installation progress to one short sentence and the result; do not narrate routine state transitions.

## Boundaries

Lessons belong in dedicated learning folders. Work projects receive only namespaced reusable skills, never lesson/demo files, names or progress. Do not write to CLI-managed projects or modify existing CLI/config/account data. Local paths, names and files stay local; the hosted service receives only content and operation identifiers.

The optional account connection currently exposes availability only (`not_configured`). Sign-in, paid material and account progress are unavailable. Do not claim otherwise. Missing tools, conflicting files or mismatched hosts are errors to explain, not reasons to change permissions or fall back to the old installer.
