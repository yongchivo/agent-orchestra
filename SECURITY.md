# Security policy

Uno (package `uno_orchestra`, formerly agent-orchestra) runs AI coding agents against repositories it treats as untrusted. The
controls and the risks that remain are described in the README's "Security controls",
"Threat model" and "Residual risks" sections.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository
and choose **Report a vulnerability**. Include the version or commit, steps to reproduce,
and what an attacker gains. You will get an acknowledgement within 7 days.

In scope: anything that lets a malicious repository, a prompt-injected agent, an agent's
artifact handed to another role, the planner, or a client of the dashboard or the control UI do
more than the README says they can (run commands on the host, read files outside the worktree,
merge out-of-scope changes or reach the user's branch without approval, widen a role's
permissions or paths, exfiltrate data, execute script in a page, bypass the limits).

Out of scope: running the dashboard with `--host 0.0.0.0`, disabling controls on purpose,
and vulnerabilities in Claude Code itself (report those to Anthropic).

## Supported versions

Only the `main` branch receives fixes.
