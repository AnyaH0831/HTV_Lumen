# Conversation card, fewer approvals, and how eagerly Lumen speaks

## Autonomy levels (`LUMEN_AUTONOMY`)

The original policy asked the signer to approve almost everything, which is what made the voice feel
slow and naggy. There are now two levels, decided in **code** (`server/policy.py`, `assess(..., autonomy=)`):

| | `fast` (default — demo) | `tiered` (clinic-safe, original) |
|---|---|---|
| Clear phrase | speak immediately (after the cancel countdown) | speak immediately |
| Interpreter unsure (`uncertain`) | **speak anyway** (recorded as an *advisory*) | ask first |
| Non-critical contradiction (YES+NO, HOT+COLD, BAD+BETTER) | **speak anyway** (advisory) | ask first |
| Critical word (EMERGENCY, ALLERGIC, HELP, FALL, POLICE, MEDICINE) weak (< 0.80) | ask first | ask first |
| Critical word a close call | ask first | ask first |
| Mean confidence < `LUMEN_LOW_MEAN` | ask the signer to sign again | ask the signer to sign again |

So in `fast` mode the **only** things that still stop and ask are a critical word that is weak or a
close call. Everything else is voiced at once; the doubts that were skipped are logged in the plan event's
`advisories` field for the dashboard/paper. Texting the emergency contact remains a separate approval in
**both** modes (see `docs/NOTIFICATIONS.md`).

- Set `LUMEN_AUTONOMY=tiered` (or pass `autonomy="tiered"` in tests) for a clinic where a wrong word
  costs more than a slower voice.
- `server/eval_policy.py` always evaluates the **tiered** policy, so the safety numbers in the paper
  describe the most conservative behaviour.

### Measured, not guessed
`server/eval_policy.py` is a **model** over scripted token distributions, not real users. Before
quoting any number, run the evaluation harness (`Lumen_TODO.md` §2) with scripted scenarios and report:
critical-detail changes, false speech per idle minute, needless asks, end-of-sign-to-speech delay, and
cancel rate — for `fast` vs `tiered` vs "raw words".

## What changed for the person in the headset
One translucent card replaces the three panels (use `?ui=classic` for the old layout, `?card=left|right` to move the card).
- Doctor speech (left) and what the system says for the signer (right), a live signing line, and a state chip: Ready, Listening, Working it out, Speaking in N, Check this.
- It sits at arm's length (0.5 m), a little above the signing space, and stays where it was put so you can poke it. It re-centres only after a big turn, walking away, or a posture change, and never while a fingertip is near it. It fades while a hand is in front of it.
- The signing line always shows your newest words, and the same sign again within 2.5 s counts as one word (shown as "PAIN x3").
- After a detection, a "Not sign" button appears for 6 s. Pressing it removes the word and saves that clip as a "not a sign" training example, so false triggers improve the next model. This uploads that clip's hand joints (no video), only when you press it.
- Controls appear under it only when they apply: Cancel / Speak now (while words wait or during the countdown), Yes / No (when asked). Signing YES or NO also answers.

## Who decides what happens (server/policy.py, decided in code, not in a prompt)
| Outcome | When | What the signer sees |
|---|---|---|
| speak | clear phrase, no critical doubt — **or any non-critical doubt in `fast` mode** | sentence appears with a countdown (default 1.5 s); Cancel stops it |
| ask | a critical word (EMERGENCY, ALLERGIC, HELP, FALL, POLICE, MEDICINE) is a close call or weak (< 0.80); **in `tiered` also** interpreter doubt or contradiction (YES+NO, HOT+COLD, BAD+BETTER) | a spoken read-back; sign YES or NO |
| repeat | mean confidence < `LUMEN_LOW_MEAN` (0.45) or nothing recognised | "Please sign again" and nothing is spoken |

Texting the emergency contact is never automatic. A clear EMERGENCY/HELP is spoken at once, then the signer is asked separately about texting. If the phrase needed a read-back anyway, one question covers both. An approved read-back alone never unlocks texting (`rt.confirmed` is set only by purpose `send_message`).

Whatever is spoken may only use words the signs support (`policy.grounded`). A planner sentence with a duration, severity, negation or cause is replaced by the plain rule sentence.

## Timing
Signs are sent as they are recognised, each with its runner-up candidates. After a pause (`LUMEN_QUIET_S`, default 1.5 s) the phrase goes to the pipeline. If Gemini says the signer is probably not finished (`complete=false`), it waits once more (`LUMEN_EXTEND_S`, default 1.5 s). Signs made while the agent works are kept for the next sentence.

| Variable | Default | Meaning |
|---|---|---|
| `LUMEN_AUTONOMY` | `fast` | `fast` speaks every non-critical phrase; `tiered` asks on any doubt |
| `LUMEN_QUIET_S` | 1.5 | pause that ends a sentence |
| `LUMEN_EXTEND_S` | 1.5 | one extra wait if the interpreter thinks more is coming |
| `LUMEN_WAIT_FOR_COMPLETE` | 1 | `0` disables the extra wait (fastest; the Gemini call is cached anyway) |
| `LUMEN_SPEAK_DELAY_S` | 1.5 | cancel countdown before speaking |
| `LUMEN_LOW_MEAN` | 0.45 | mean confidence below this asks the signer to sign again |
| `LUMEN_DEDUPE_S` | 2.5 | the same sign again within this window is one word |
| `LUMEN_MAX_WAIT_S` | 10 | hand a sentence over after this long even if signing never pauses |
| `LUMEN_MAX_QUEUE` | 8 | ... or once this many signs are waiting |
| `LUMEN_CONFIRM_TIMEOUT_S` | 25 | how long a yes/no question waits (no answer counts as No) |

Headset URL parameters: `?ui=classic`, `?card=left|right`, `?dedupe=2.5` (seconds), `?tent=0.60` and `?bg=0.25` (an uncertain word is accepted only above this confidence and below this "not a sign" probability), `?strict=1` (experimental stricter sign gate: hands higher, held longer; compare false words per idle minute before trusting it).

Speech goes to the clinician's dashboard laptop (falls back to every client if no dashboard is open). The patient's headset does not speak it.

## Measuring it (for the paper or the pitch)
Have people sign scripted scenarios with a known intended meaning and report: spoken sentences that change a critical detail, false speech per idle minute, how often it asked when it did not need to, delay from the end of the last sign to speech, and how often signers cancel.

## Why recognition could appear to stop
Two causes were fixed. (1) A yes/no question that ended without you answering on the headset (timeout, approved on the dashboard, run finished) left the headset in "answer only" mode, so every sign was treated as an answer attempt and no words were added. Questions now close on `confirm_closed`, on `agent_done`, and on their own expiry. (2) The signing line used to show the oldest words, so with a long queue new words fell off the right edge. Watchdogs also clear a stuck busy flag (90 s) and a stuck countdown.

## ML improvements to make the voice both faster and more trustworthy

The autonomy change above fixes the *policy*, not the model. The thing that actually lets `tau_auto` rise
(so fewer asks are needed at all) is recogniser quality on **deployment-shaped** data. Recommended, roughly
in order of payoff:

1. **Close the domain gap — record your own signers.** The only data that matches the Quest sensor is
   Quest data. The demo model sees one signer; `make loso` warns below four. Every new signer improves
   both accuracy and the calibration that the thresholds are fit on. This is the single biggest win.
2. **Collect background negatives aggressively.** The headset's **Not sign** button already uploads the
   rejected clip's joints as a `__background__` training example (`assist.js`/`recorder.js`). False
   triggers are what make `fast` mode feel unsafe; more negatives let you raise `tau_auto` and lower
   false words per idle minute without losing recall.
3. **Recalibrate thresholds on-device.** `tau_auto`/`tau_confirm` come from validation, not the test
   signer (`train.py`). Re-run `eval_policy.py` and the threshold sweep on clips from the actual
   deployment signers so `fast` mode's skipped approvals are backed by measured risk, not a guess.
4. **Per-signer adaptation.** Temperature and thresholds are fit per dataset. A tiny per-signer bias
   correction (a few labelled clips) would cut both needless asks and critical-word weakness.
5. **Augmentation & parity hygiene.** `augment.py` already mirrors, rotates, time-warps and adds noise on
   raw clips. Add temporal jitter and speed variation; keep the JS↔Python feature parity test green — any
   drift silently degrades everything downstream.
6. **Address the domain gap explicitly.** Run transfer conditions B/C (`--extra-train`, `--init`) to
   measure whether Google-ISLR pretraining actually helps held-out Quest signers, then decide whether to
   keep spending on it. 23/30 glosses map; HELP, PAIN, COLD, ALLERGIC, MEDICINE, DOCTOR, EMERGENCY are
   Quest-only and need their own data.
7. **Temporal / sequence modelling.** Isolated-sign recognition can't see grammar. A CTC/transformer
   sequence model over continuous signing is the long-term fix for both speed and meaning, but it needs
   continuous data that this repo does not yet collect.

Two causes were fixed. (1) A yes/no question that ended without you answering on the headset (timeout, approved on the dashboard, run finished) left the headset in "answer only" mode, so every sign was treated as an answer attempt and no words were added. Questions now close on `confirm_closed`, on `agent_done`, and on their own expiry. (2) The signing line used to show the oldest words, so with a long queue new words fell off the right edge. Watchdogs also clear a stuck busy flag (90 s) and a stuck countdown.
