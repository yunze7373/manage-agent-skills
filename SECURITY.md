# Security

Please report suspected vulnerabilities privately to the repository owner until
a public security contact is configured.

The manager edits agent configuration and invokes installed Copilot/OpenClaw
CLIs. Antigravity can move whole directories under a selected global root.
Backups can contain secrets from the original config; keep them private.
Native command batches and presets are not transactions across runtime errors.
Review dry-run output before mutations. Tests and bug reports must use temporary
configuration files and must not include tokens, credentials, or full private
configuration contents.
