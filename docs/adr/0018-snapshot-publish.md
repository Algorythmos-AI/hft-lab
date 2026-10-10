# ADR 0018: Publishing the status snapshot: one reading, checked, signed, and sent only with the owner's key

- **Status:** accepted, 2026-10-11
- **Context:** F2, recorder; the first install. The contract it publishes is `contracts/hft-snapshot.v1.schema.json`

## Context

The contract for what this desk may tell the outside exists, with its rules: no free text, closed objects,
paper only, optional parts absent until they are real ([contracts](../../contracts/README.md)). The status
site exists too, and its page for this desk says that nothing has been published yet. What was missing is the
publisher.

The site is shared with the other desks. Its ingest takes a signed POST, and it decides which desk a body
belongs to from the `schema` field inside the signed body. How it checks a signature was read from the
sibling repository's dashboard at commit `1b23931` (`dashboard/src/lib/hmac.ts` and `ingest.ts`), not guessed.

## Decision

1. **The snapshot is built from the status reading** ([ADR 0017](0017-host-layer.md), decision 6). The
   screen on the host and the page on the site come from the same files read once, so they cannot disagree.

2. **What is in it now.** Identity, the expected windows, the desk, the phases and health, always; and the
   recorder part once something has been recorded on the paper venue.
   - `desk.mode` is the literal `paper`. Nothing handed to the builder can choose another value.
   - `desk.stage` is what the host shows evidence of: `foundation`, then `recording` once the recorder has
     run against the paper venue, then `shadow` once the shadow runner has a session on gateway quotes.
   - `health.reasons` are the codes of the status problems. A problem's sentence stays on the host.
   - `phases` come from `config/ops.yaml`, and a test holds them to the roadmap's table of gates.
   - `version` is the commit of the code that built it, read from git's own files.
   - `as_of` is the time the snapshot was built, not the recorder's last beat. The site orders snapshots by
     it, and a recorder that has stopped must not make every later snapshot look older than the last.
   - **Absent, not zero:** `pairs`, `sleeves`, `books` and `learning`. A store written by a dry run of the
     fake broker is not published as the recorder either; it is a reason code instead.

3. **`expected_windows` lists two weeks**: the trading week that is open or the next one to open, and the
   week after it, from the calendar (Sunday 17:15 to Friday 17:00 New York, in UTC). With only the first, a
   host that died on a Friday evening would leave the site with no window to expect it in.

4. **It is checked against the contract before it is printed or sent**, by a checker in
   `hftlab.publish.contract` that understands exactly the keywords the contract uses and refuses a contract
   that uses any other. The repository's test of the contract file uses the same code. There is one checker.

5. **A dry run is the default.** `python -m hftlab.publish` prints the snapshot. `--send` sends it, and needs
   three things from the environment: `HFT_PUBLISH_URL`, `HFT_PUBLISH_KEY_ID` and `HFT_PUBLISH_KEY`. With any
   of them missing it refuses and says which. It also refuses a key id that is not this platform's own, and
   an address that is not `https` or is on this machine. Neither the address nor the key is ever printed.

6. **The request is the site's own form.**

   | Header | Value |
   |---|---|
   | `x-wt-timestamp` | Unix seconds when it was signed |
   | `x-wt-signature` | `sha256=` and the hex of HMAC-SHA256 over `<timestamp>.<body>` with the key |
   | `x-wt-key-id` | Which key signed it |

   The body is the snapshot in one canonical form. The header names are the receiving site's, which is why
   they do not carry this platform's prefix. The site refuses a timestamp more than five minutes from its
   own clock. One attempt is made: the timer runs again in five minutes, and that is the retry. An answer
   that the site already holds a newer snapshot is not a failure.

7. **The publish job is also the watch.** `hft-publish.service` first runs the status check with alerts,
   then sends. A failing check does not keep the snapshot from going out: a bad state is exactly what the
   page should show.

## Consequences

- **Two decisions are the owner's and are open.** The signing key, and which key id the site accepts for
  this desk. Until both exist on the host and on the site, `--send` refuses or the site answers 401, and
  the page keeps saying that nothing has been published. The runbook has the steps
  ([publish](../runbooks/publish.md)).
- **The site has to know this schema before the first send.** At the commit that was read, the ingest files
  a body whose `schema` it does not know under another desk and then refuses it as invalid. Nothing of
  another desk's is overwritten, and nothing of ours is stored. The contract's own rule applies: the
  consumer takes the file first, then the publisher sends ([contracts](../../contracts/README.md)).
- **Nothing has been sent.** The signature is tested against the format above with a fixed example, and the
  transport in every test is a stand-in. The first real send is the test of the address, the key and the
  site.
- **The site's own watch is the off-host dead-man.** Silence inside an expected window is its fault to
  raise. The alerts of ADR 0017 are the on-host half.
- **Per-pair figures** (update rate, median spread) are in the contract and not in the snapshot yet: they
  need the recorded quotes to be read, which the status reading does not do. They come with the first
  analysis of recorded data, as an addition the contract already allows.
