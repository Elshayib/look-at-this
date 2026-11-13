## Summary

<!-- Briefly describe what this PR changes and why. -->

## Changes

- 

## Platform & Surface Testing

<!-- Check all environments you tested directly, or note mocked/unit coverage only. -->

- [ ] Windows (Win32 capture / UIA / GDI overlay / `RegisterHotKey`)
- [ ] macOS (`screencapture` / JXA / Desktop shortcuts)
- [ ] Linux (`grim`+`slurp` Wayland or `import`+`wmctrl` X11)
- [ ] Hermes Desktop UI (`desktop/plugin.js`)
- [ ] CLI / `/look` & Agent Tools

## Contributor Checklist

- [ ] `python -m unittest discover -s tests -v` passes
- [ ] `node --check desktop/plugin.js` passes
- [ ] `hermes plugins validate .` passes with 0 errors and 0 warnings (if Hermes is installed)
- [ ] Privacy & ephemerality invariants respected (excluded windows/occlusion checks intact, temporary PNGs deleted after turn, `[look-at-this packet]` delimiters neutralized, no unauthorized network calls)
- [ ] Hermes Catalog rules respected (no self-updating code, Desktop imports `@hermes/plugin-sdk` only, no private Hermes core imports or runtime monkeypatching)
- [ ] If bumping the version, updated all 5 files (`plugin.yaml`, `skills/look-at-this/SKILL.md`, `__init__.py`, `lat/__init__.py`, `dashboard/manifest.json`) and `CHANGELOG.md`
