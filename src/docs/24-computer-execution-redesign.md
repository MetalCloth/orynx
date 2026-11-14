# Proposed computer execution redesign

[Handbook index](README.md) · [Current implementation](21-cua-main-loop.md) · [Tool cheat sheet](23-computer-use-cheat-sheet.md)

**Status: proposal, recorded 4 November 2026.** This captures the design discussed after
the current implementation at `89fd3cb`. There is no new executor or measured speedup
from this proposal yet.

## What we already have

The model chooses tools inside Oryn's ordinary conversation loop. `computer_act` accepts
1–5 inputs grounded in the current observation, dispatches them, and returns fresh
state for the next model decision. Certain same-window text-entry patterns use one
Dotool process. Other sequences check the target between inputs. A structured native
`wait_for` can check an outcome before returning; `expected_result` is only prose for
the model. Named task records preserve progress but do not independently judge success.

The [current guide](21-cua-main-loop.md) documents the implemented focus, coordinate,
transition, recovery, and history behavior. [Trace logging](22-computer-tracing.md)
provides model/tool timing and optional images in a separate folder for each traced
turn. These are existing capabilities, not results of the proposed redesign.

## Where input comes from

| Source | Current local desktop behavior | Boundary |
| --- | --- | --- |
| Hyprland metadata | Window identities, geometry, focus, and workspace listing | Describes windows, not page contents |
| Native accessibility / AT-SPI through CUA | Bounded window tree, numbered controls, and supported native state checks | Depends on what the application exposes; desktop summaries are read-only |
| Screenshots | Native-size desktop/window images and requested desktop crops | Visual targets still need interpretation; a stable image alone does not prove success |
| Local browser DOM | No connection to the user's local Brave DOM | Proposed capability, not something every browser task receives today |
| Remote browser snapshot | Separate Firecrawl `browser_*` tools observe and control their remote session | Does not describe the local browser or inherit its logged-in state |

Use a fresh structured target when available and screenshots when the target or state
needs visual interpretation. Remote browser work must remain distinct from local
desktop work. None of these sources universally describes every interface.

## The proposed change

Keep the provider/authentication, chat interfaces, conversation loop, context handling,
session storage, and trace infrastructure. Refactor the computer execution layer in a
small experiment instead of rebuilding the whole product.

The model would supply a short plan with explicit conditions. Ordinary code would run
predictable UI inputs, wait for supported conditions, and resolve fresh targets after
a UI change. It would return to the model when evidence is missing, the target is
ambiguous, a condition times out, or the next step needs visual judgment. Failed or
uncertain actions must not be blindly replayed.

For example, a proposed segment could open a menu, wait until a uniquely identified
“Settings” control appears, resolve that control from fresh accessibility data, click
it, and check for the settings window. Today, the model must inspect the newly opened
menu before sending the dependent click. The proposal would avoid that extra model
decision only when code can actually check and resolve the new target. A sentence
such as “Settings appeared” does not itself create a visual detector.

This extends execution across checkable UI changes; simply raising the existing
five-input limit would not provide that behavior. It should work through general UI
contracts, without app-name branches, fixed coordinates, or task-specific scripts.
The plan format and supported conditions still need to be designed and reviewed
before implementation.

## First experiment and later work

1. Build one complete conditional segment using existing observation/input adapters.
   Show its supported conditions, failure behavior, cancellation, and trace evidence.
2. Compare representative browser, native-app, and visual-only tasks with the same
   model, effort, speed setting, and starting state. Separate cold and warm runs.
   Measure completion, elapsed time, model calls, tool/capture time, wrong inputs,
   and recovery. A faster wrong result is not an improvement.
3. Expand only where the experiment improves results. Keep visual-only tasks able to
   return to the existing observe–decide–act loop.

Continuous UI state, persistent event listeners, reusable validated workflows, a local
browser DOM adapter, and a separate grounding model are later options. They are not
implemented by this plan, and none is assumed necessary before the first experiment.
