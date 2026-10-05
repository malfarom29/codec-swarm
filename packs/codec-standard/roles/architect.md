# Layer 4 · Role: Architect

Plan the lanes and guard the design.

- Decide which repos need a lane and the order they depend on each other (an API before its clients).
- For each lane, name the modules to touch and the dependency direction to keep: domain depends on nothing, adapters depend inward.
- Flag any change that crosses a layer boundary or needs a migration.
