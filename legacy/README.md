# Legacy

`axiom_launch_and_monitor.py` is the original BAIT-style wrapper around the Axiom
`start_test` / `/events` REST interface, with its `notification/` email dependency.

It is **not maintained** and is **not part of the supported working path**. The current
supported tool is `../axiom_flow.py` (see the top-level `README.md`).

These files are kept only because the external `axiom` skill references
`axiom_launch_and_monitor.py` by name. The script adds its own directory to `sys.path`
at import time, so `from notification...` resolves correctly from within this folder.
