<!-- eloise:begin -->
<!-- Rendered by `eloise sync --compiler` from eloise/conventions.md.
     Edit that, not this block: anything written outside it is yours and
     is never touched. -->
## How this project is built (Eloise)

- Dead or inert UI is a bug: remove it, never stub it.
- App UX must be easy for anyone: plain language, daily rituals as numbered steps, one subtle action per section, onboarding for first-timers.
- Prefer constant in-place UI over auto-dismiss toasts; informational text at body size, not headline.
- Landing and marketing copy is professional, never 'AI-ish': no gradients, emoji, fake stats or buzzwords; flat white and navy, honest copy, CTAs that match real flows.
- Ghana apps favour speed over compliance theatre; NHIS and drug-logbook integration optional.
- On 'execute', do it: commits, pushes, private repos, suites. Leaving those to the owner is the failure.
- Project pages are drafted into `fleeting/`; `projects/` is written by the owner.
- Dependency bumps go patch and minor without asking; a major waits. `vendor/tauri-plugin-sql` is never bumped.
- Missing repos are cloned and their dependencies installed without asking — local, nothing leaves the machine.
- A red suite is maintenance, not an audit: fix, verify, commit and push in one pass.
- An engine update is rewired without asking, with the hashes recorded so the restore is provable.
- Audit asks, breakdown does not: red, uncommitted or unpushed is fixed in the pass; what changes what the app *is* waits.
- Four still wait for the owner: a force-push, deleting outside /tmp, billing or credentials, the Praxis production schema.
- Audits are staged: audit → list only → wait for 'yes' → execute. Fixes are batched blocker → high → medium → low with todos, every suite verified before reporting, and the tree is committed and pushed at the end of each batch.
- In Hermes, key-like identifiers are redacted in tool output — verify with node --check, git diff or md5, never read_file.
<!-- eloise:end -->
