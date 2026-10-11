# Security policy

agent-orchestra runs AI coding agents against repositories it treats as untrusted. The
controls and the risks that remain are described in the README's "Security controls",
"Threat model" and "Residual risks" sections.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository
and choose **Report a vulnerability**. Include the version or commit, steps to reproduce,
and what an attacker gains. You will get an acknowledgement within 7 days.

In scope: anything that lets a malicious repository, a prompt-injected agent, or a client of
the dashboard do more than the README says they can (run commands on the host, read files
outside the worktree, merge out-of-scope changes, exfiltrate data, execute script in the
dashboard, bypass the limits).

Out of scope: running the dashboard with `--host 0.0.0.0`, disabling controls on purpose,
and vulnerabilities in Claude Code itself (report those to Anthropic).

## Supported versions

Only the `main` branch receives fixes.
