# Work Package C agent guide

Read `README.md` first, then `docs/DECISIONS.md`, `docs/BC_CONTRACT.md`, and
`docs/CD_CONTRACT.md` before changing contracts or planning behavior.

## Invariants

- Do not import Work Package A or B implementation modules. Integrate only through versioned contracts/adapters.
- Do not edit Work Package B from this project.
- B supplies declared environmental effects; C computes final vessel speed. Never infer speed loss from risk score or confidence.
- Never treat missing risk as zero, extrapolate beyond a risk window, mix contexts, or silently snap endpoints.
- Keep scenario/vessel configuration path-injected so it can move to shared `demo_scenarios/contracts` later.
- Demo vessel values are unvalidated and must never be described as calibrated or safe for navigation.
- Preserve generation/request/revision publication fencing and immutable contract models.
- Use Mamba for native runtime libraries and uv for Python dependencies/lockfile.

## Required verification

Run `make check` after code changes. Add a focused regression test for every contract, ETA-sampling, planner, replanning, or publication bug. If legacy behavior changes, run the external artifact integration test against the user-provided `交付包.zip` when it is available.

