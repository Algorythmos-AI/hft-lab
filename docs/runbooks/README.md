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
| [Trader](trader.md) | Starting it in shadow, reading the stages, kill and flatten with a trader running, a reconciliation break, resetting a latch, replaying a day, the tapes and their quota, sleeve state and what "started cold" means |
| [Backup](backup.md) | Handing the journals, the trader's folder and the model registry away every night, what is left out and why, and checking that a backup can be read back |
| [Reports](reports.md) | The daily summary and the weekly scorecard: what each says, making one by hand, and the timer that sends them |
| [Deploy](deploy.md) | Putting another commit of `main` in service through the deploy gate: adopting the layout once, the dry run, the deploy, what each ending means, and what to do when the unit files changed |
| [Rollback](rollback.md) | Going back to the code that was in service before the last deploy, by the same command and the same gate |
| [Publish](publish.md) | Sending the status snapshot and the pages, and the two decisions that are still the owner's |
| [Learning](learning.md) | The weekly cycle: setting the two times, reading a report, what `keep: rule not in force` means, how a rule comes into force, a rollback and the rollback drill, the drift look, and what to do when training fails |
| [History import](history-import.md) | Reading tick files you exported yourself into the store of imported history: the description file, the dry run, the report, and what each refusal means |
| [IBKR capability probe](ibkr-probe.md) | Measuring what the paper account can do: installing the gateway, the paper login, the command, reading the report |

None of the host runbooks has been followed on the host yet: nothing is installed. The deploy gate, the
reports, the tape's quota and the wider backup are written and tested off the host only. Each says so where it
matters, and each is corrected in the pull request that follows its first use.

Rules for every runbook:

- Written for the owner at an inconvenient hour: no background, no choices that need the design in your head.
- No secret, account id, host name or address in the text. It says where to find them.
- Automated contributors never log in to the broker, never type credentials, and never enable or start the
  platform's units. A runbook step that needs any of those is the owner's.
- A runbook that was followed and found wrong is fixed in the same pull request as the incident note.
- Every step has the command, what you should see, and what to do if you do not.
