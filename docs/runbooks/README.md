# Runbooks

A runbook is what the owner follows when something needs a person: one page, numbered steps, the exact
commands, and how to tell it worked. Each is written before the exit gate of the phase that first needs it,
and is exercised once as a drill.

## Written

| Runbook | For |
|---|---|
| [Install](install.md) | The pre-flight day of measurements on the host, the install, and the first start of the recorder |
| [Recorder](recorder.md) | Starting and stopping it, reading the status, checking a recorded day, a gap, the quota, a full disk, and what happens after a kill |
| [Shadow run](shadow-run.md) | Running the sleeves over recorded days or on live quotes, verifying the journal, and making the feasibility map |
| [Weekly login](weekly-login.md) | What the gateway needs on the host, and its once-a-week paper login |
| [Gateway down](gateway-down.md) | The broker's gateway stops or will not connect |
| [Kill and flatten](kill-and-flatten.md) | The `KILL` and `FLATTEN` switches, and stopping what runs today |
| [Backup](backup.md) | Handing the journals away every night, and checking that a backup can be read back |
| [Rollback](rollback.md) | A deploy to another commit of `main`, and going back to the one before |
| [Publish](publish.md) | Sending the status snapshot and the pages, and the two decisions that are still the owner's |
| [IBKR capability probe](ibkr-probe.md) | Measuring what the paper account can do: installing the gateway, the paper login, the command, reading the report |

None of the host runbooks has been followed on the host yet: nothing is installed. Each says so where it
matters, and each is corrected in the pull request that follows its first use.

## To be written

| Runbook | For |
|---|---|
| Reconciliation break | The broker's positions and ours disagree |
| Kill and flatten, from paper trading on | What each switch does to working orders and positions. The page exists; this part waits for the risk gate (F4) |
| Rollback of a model | Going back to the previous champion model (F6) |

Rules for every runbook:

- Written for the owner at an inconvenient hour: no background, no choices that need the design in your head.
- No secret, account id, host name or address in the text. It says where to find them.
- Automated contributors never log in to the broker, never type credentials, and never enable or start the
  platform's units. A runbook step that needs any of those is the owner's.
- A runbook that was followed and found wrong is fixed in the same pull request as the incident note.
- Every step has the command, what you should see, and what to do if you do not.
