# Plan: reading and clearing codes, and full diagnostics

Status: steps 1, 2 and 3 are built, and step 4 apart from writing data (2E). The policy table, the vehicle-state checks and the sender are in
`pandacapture/policy.py`. The table holds the standard OBD reads (`pandacapture obd`, `pandacapture
codes`), the UDS reads (`codes --uds`, `modules`, `did`), and clearing codes (`codes --clear`). The rest is planned. `send` and `replay` are separate: they send the frames you give them.

The aim is an open source diagnostic tool: read and clear codes, read live data from any module, and
the bidirectional functions a dealer tool has (actuator tests, routines, resets). The rule that makes
it safe: **anything beyond reading only goes out with the engine off and the car stopped**, checked
from the car's own signals right before each request, and undone if that changes.

## Why not filter by bus type

A bus's type can't be told reliably from its traffic, and it doesn't decide what's safe:

- **Standard OBD requests are harmless on any bus.** On most modern cars the engine computer sits on
  the powertrain bus, and the gateway passes the OBD port's requests onto it anyway. A request sent
  straight onto the powertrain bus reaches the same module.
- **The dangerous services are just as dangerous on the diagnostic bus.** An ECU reset or an actuator
  test does the same thing whichever wire it arrives on.
- **The one real difference is the gateway.** Some gateways check requests from the OBD port (FCA's
  Secure Gateway; makes that refuse a clear while moving). Sending on the powertrain bus skips those
  checks, which is why PandaCapture does its own state checks instead of relying on the car's.

So the bus type is shown as a warning ("this bus carries powertrain traffic: requests bypass the
gateway"), not used as a gate. What protects people is the policy table below.

## The policy table

Every request PandaCapture can build comes from one table in the code. A request that isn't in it
can't be built: there's no raw "send any diagnostic request" path. Each entry says:

- the service and the sub-functions allowed
- its tier (below): the vehicle state it needs
- the confirmation word, if any
- what undoes it (the cleanup sent if the state changes or the program stops)

### Tiers

| Tier | Needs | Services |
|---|---|---|
| **Read** | the bus, nothing else. Discovery scans stay key-on or idle (up to 900 rpm), as today | OBD modes 01 (live data), 02 (freeze frame), 03 (stored codes), 06 (monitor results), 07 (pending codes), 09 (vehicle info), 0A (permanent codes). UDS `19` (read codes), `22` (read data by identifier), `3E` tester present while reading |
| **Engine off** | engine off and car stopped (below), held for the whole session, and a typed confirmation | OBD 04 (clear codes); UDS `14` (clear codes), `10 03` (extended session), `11` (ECU reset), `28` (communication control), `85` (code setting on/off), `2F` (input/output control: actuator tests), `31` (routine control), `2E` (write data by identifier) |
| **Not in releases** | | `10 02` (programming session), `34`–`37` (download, upload, transfer: flashing), `27` (security access) |

Why the last row stays out, even with the engine off: flashing and programming sessions can leave a
module unable to start whatever the engine is doing, and recovering it needs the manufacturer's tools.
Security access needs each manufacturer's seed-to-key algorithm, which PandaCapture doesn't include.
Engine-off services that a module only allows after security access will be refused by the module.

## "Engine off and car stopped"

All of these, from fresh readings (under 0.5 s old), checked right before every engine-off request:

1. **Engine speed is exactly 0**, from the address map's broadcast RPM signal, or OBD PID 0C when the
   map has none. An unknown engine speed counts as running.
2. **Vehicle speed is 0**, from the map's speed signal or OBD PID 0D. This is what covers hybrids and EVs
   (0 rpm while driving on the motor) and auto stop-start (0 rpm at a light, about to restart).
3. **In Park, if the map knows the gear.** When the map has a gear or park signal, it must say Park.
   On a car whose map has none, the confirmation says so.
4. **Key on.** Implied by the modules answering.

### If the state changes during a session

The engine starts, the car moves, or engine speed stops being known. PandaCapture then:

1. Stops sending anything new and stops tester present.
2. Undoes what's active, from the table's cleanup column, most recent first:
   - `2F xx 00`: return control to the ECU
   - `31 02`: stop routine
   - `28 00`: re-enable communication
   - `85 01`: turn code setting back on
   - `10 01`: back to the default session
3. Ends the session. Starting again needs a new confirmation.

### If PandaCapture stops

A module drops back to its default session by itself a few seconds after its last tester present (the
S3 timer, about 5 s). That ends extended-session effects: actuator control, routines, communication
control. PandaCapture only sends tester present from its own loop, and only while the state holds. So a
crash, a closed laptop or a pulled cable undoes things within seconds. It works the same way as the
panda's transmit heartbeat.

## Confirmations and logs

- **Read tier:** the existing OBD acknowledgement, once per run.
- **Clearing codes:** reads and shows the codes first (stored, pending, permanent, and freeze frame),
  explains that readiness monitors reset (an emissions inspection fails until they're ready again) and
  that learned values may reset. Then needs `CLEAR` typed.
- **Other engine-off services:** shows the exact request, the module it goes to, and what it does. Then
  needs `ENGINE OFF` typed, once per session.
- **Logs:** every request and answer goes to the transmit log, with the vehicle state at the time.

## Adapters

| Adapter | Read tier | Engine-off tier |
|---|---|---|
| Panda (PandaCapture firmware) | yes, on any bus with traffic | yes |
| ELM327 | yes (`ATSH` addresses one module) | yes. Its own limits: long writes may not fit |
| RP1210 / J2534 | needs a transmit path in the bridge (receive-only today) | after that |

## Order of work

1. **Done:** the policy table and the state checks, with today's mode 01 as its only entry. The panda
   path and the ELM327 link both send through it, and the ELM327 link refuses any other frame.
2. Read tier.
   - **Done (2a):** codes (03/07/0A), the check-engine light, freeze frame, vehicle info, with ISO-TP for
     long answers. ISO-TP flow control is in the policy as the one transport frame: exactly `30 00 00`,
     to the module that's answering.
   - **Done (2b):** UDS `19` (its reporting sub-functions only) and `22` (one to three identifiers), per
     module. Modules are addressed 700-7F7, answering at +8. An id the bus uses for ordinary traffic (frames
     that don't look like diagnostics) is never sent to, nor is 7D7, whose answers would land on 7DF.
     An ELM327 is told to listen on the module's answer id (`ATCRA`) for modules outside 7E0-7E7.
3. **Done:** clearing codes (OBD 04, UDS `14 FF FF FF`) behind the engine-off checks.
   - The sender refuses a service with a confirmation word until the user has typed it this session.
   - Before each clear request, engine speed and vehicle speed must be read anew, after that moment: a
     reading from before, however recent, doesn't count, since the engine may have started since.
4. **Done, apart from 2E:** the other engine-off services (`pandacapture diag`, `diagsession.py`).
   - Per-request tiers: `10 03`, `85 02`, `28 01-03`, `11`, `2F … 01-03` and `31 01` are ENGINE_OFF with
     `ENGINE OFF` typed. What undoes them (`10 01`, `85 01`, `28 00`, `2F … 00`, `31 02`/`03`) is a READ, so
     it goes out even after the engine has started.
   - One worker thread owns the link: it checks the state every 0.25 s and sends tester present (`3E 80`,
     itself ENGINE_OFF) every 2 s only while the state holds. It undoes newest first when the state fails.
   - It tracks explicit stops and resets.
   - **Still to do: `2E`** (writing data). It changes a module permanently, with no natural undo, so it needs a
     read-back of the old value first, kept as the undo, and its own confirmation.
5. A transmit path for the RP1210/J2534 bridge.

Each step is tested on a simulated car before a real one, as the OBD scan was.
