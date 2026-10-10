# Runbooks

A runbook is what the owner follows when something needs a person: one page, numbered steps, the exact
commands, and how to tell it worked. None is written yet, because nothing runs yet. Each is written before
the exit gate of the phase that first needs it, and is exercised once as a drill.

| Runbook | For |
|---|---|
| Weekly login | The gateway's once-a-week paper login |
| Kill and flatten | Stopping new entries (`KILL`) and closing every position (`FLATTEN`) |
| Gateway down | The broker gateway stops or will not connect |
| Reconciliation break | The broker's positions and ours disagree |
| Disk full | The recording quota or the host's free-space floor is reached |
| Rollback | Going back to the previous deploy, or to the previous champion model |

Rules for every runbook:

- Written for the owner at an inconvenient hour: no background, no choices that need the design in your head.
- No secret, account id, host name or address in the text. It says where to find them.
- Automated contributors never log in to the broker, never type credentials, and never enable or start the
  platform's units. A runbook step that needs any of those is the owner's.
- A runbook that was followed and found wrong is fixed in the same pull request as the incident note.
