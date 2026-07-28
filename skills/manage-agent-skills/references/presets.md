# Preset file format

Preset files are JSON and use exact selectors:

```json
{
  "version": 1,
  "presets": {
    "lean": {
      "codex": {
        "disable": ["group:azure", "group:gmail"],
        "enable": ["manage-agent-skills", "openai-docs"]
      },
      "claude": {
        "disable": ["legacy-context"],
        "enable": ["manage-agent-skills"]
      },
      "copilot": {
        "disable": ["unused-skill"],
        "enable": ["manage-agent-skills"]
      },
      "openclaw": {
        "disable": ["browser-tools"],
        "enable": ["manage-agent-skills"]
      },
      "hermes": {
        "disable": ["shell-tools"],
        "enable": ["manage-agent-skills"]
      }
    }
  }
}
```

Rules:

- Platform keys are `codex`, `claude`, `copilot`, `openclaw`, or `hermes`.
- `disable` runs before `enable`, so explicit enables win.
- Selectors are exact skill names, `group:<name>`, `path:<path>`, or `all`.
- A mutation requires the matching `--platform`; one invocation changes one
  platform.
- For Hermes, `--hermes-scope` applies to the entire preset invocation.
- Always preview with `--dry-run`.
