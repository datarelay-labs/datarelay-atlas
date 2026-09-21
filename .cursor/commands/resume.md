# Compatibility shim for Engineering System adoption checks

Engineering System >=1.3.0 expects `.cursor/commands/resume.md` to exist as the
session-continuity file.

In this repository the **canonical** slash command for the Engineering System
work-resume workflow is `/work-resume` (`.cursor/commands/work-resume.md`).

Do **not** treat Cursor's built-in `/resume` as the DataRelay Engineering
System workflow, and do not redefine `/resume` as canonical here.

Operators and the Autonomous Work Controller must submit `/work-resume`.
