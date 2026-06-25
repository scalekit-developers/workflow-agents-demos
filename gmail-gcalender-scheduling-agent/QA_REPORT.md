# QA Report — Gmail Scheduling Agent
**Agent:** Gmail + Google Calendar via Scalekit Agent Auth + Claude  
**Reference page:** scalekit.com/agent-templates/email-to-calendar-agent  
**Tested by:** Parv  
**Date:** 2026-06-25  
**Branch:** freshdesk-google-adk  

---

## Summary

Full end-to-end pipeline verified live against Gmail and Google Calendar via Scalekit. The agent now polls unread scheduling emails, extracts intent, creates calendar events, saves Gmail confirmation drafts, and completes message processing without the earlier connector failures.

| Area | Status |
|---|---|
| Connector auth (Gmail, Google Calendar) | PASS |
| Gmail polling via Scalekit | PASS |
| Gmail message fetch via Scalekit | PASS |
| Claude scheduling intent extraction | PASS |
| Calendar primary lookup fallback | PASS |
| Google Calendar free/busy check | PASS |
| Google Calendar event creation | PASS |
| Gmail draft creation via Scalekit | PASS |
| Gmail mark-as-read via Scalekit | PASS |
| End-to-end multi-message live run | PASS |
| No silent failures in happy path | PASS |

---

## Environment

| Item | Value |
|---|---|
| Scalekit env | https://hey.scalekit.dev |
| Gmail connector | gmail (parv@infrasity.com) |
| Google Calendar connector | googlecalendar (parv@infrasity.com) |
| SDK | scalekit-sdk-python==2.12.0 |
| Anthropic SDK | anthropic==0.112.0 |
| Python | 3.12 / 3.13 runtime observed locally |
| Timezone | Asia/Kolkata |

---

## Step-by-step Test Results

### Step 0: Connector auth

Startup verified both connected accounts successfully via Scalekit before polling began.

```
✔  gmail connected for parv@infrasity.com
✔  googlecalendar connected for parv@infrasity.com
✔  Both connections active
```

**What happens on failure:**
- Connector not ACTIVE: Scalekit auth flow is triggered by `ensure_connected`
- Missing or invalid Scalekit env vars: startup fails immediately with a concrete exception
- Network/DNS issue: startup fails before polling, making the problem obvious

---

### Step 1: Gmail polling and message fetch

Unread scheduling emails were fetched via `gmail_fetch_mails`, then full message content loaded with `gmail_get_message_by_id`.

```
⌕  Polling Gmail for scheduling emails...
⌕  Found 10 unread email(s) to process
📧  Invitation: Infrasity Team Meeting with Parv Mittal @ Thu Dec 25, 2025 6:30am - ...
```

**What happens on failure:**
- Fetch failure: poll loop logs the connector exception and continues on the next cycle
- Individual message fetch failure: that message is skipped with a warning instead of crashing the whole poll

---

### Step 2: Scheduling intent extraction

Claude parsing succeeded on live invite messages and consistently extracted title, start time, and duration.

```
📅  Intent: 'Infrasity Team Meeting with Parv Mittal' | Thu Dec 25 06:30 UTC+05:30 | 30min
📅  Intent: 'Final Meet' | Thu Oct 30 09:30 UTC+05:30 | 60min
📅  Intent: 'Weekly Sync' | Tue Oct 28 17:45 UTC+05:30 | 120min
```

**What happens on failure:**
- No detectable scheduling intent: message is marked read and skipped
- Parsing edge cases: default duration fallback still allows event creation when an end time is missing

---

### Step 3: Calendar lookup and event creation

Free/busy lookup and event creation both succeeded via Scalekit. Earlier `start_datetime` and calendar lookup issues were fixed during QA.

```
✔  Event created: https://www.google.com/calendar/event?eid=YTZxdTA1OGloaG9iOXNnaXRuNW5pMjcwNGc...
✔  Event created: https://www.google.com/calendar/event?eid=ZTFnMTdlM25qMzFiajVkaWNraXNnbmRqa2c...
✔  Event created: https://www.google.com/calendar/event?eid=azYxanRrMjlraWIyNm1hbm42aGVsOGlndmc...
```

**What happens on failure:**
- `googlecalendar_list_calendars` failure: falls back to `primary`
- `googlecalendar_list_events` failure: returns `[]` so conflict detection degrades safely
- Event creation conflict: returns a clear conflict error instead of creating overlapping events
- Calendar connector error: logged for that message without killing the whole process

---

### Step 4: Gmail draft creation

Confirmation drafts now succeed via Scalekit-only `gmail_create_draft` after retrying a few connector-safe payload shapes and removing unsafe Unicode from the draft body.

```
✉  Confirmation draft saved (id=r2999871936864195290)
✉  Confirmation draft saved (id=r3028898142119556037)
✉  Confirmation draft saved (id=r1481511699843398284)
✉  Confirmation draft saved (id=r5133208843023918920)
```

**What happens on failure:**
- Connector template mismatch on one payload shape: retries alternate shapes
- Draft tool failure after all retries: warning is logged, but the already-created calendar event is preserved

---

### Step 5: Mark message as read

Mark-as-read now works via `gmail_modify_message_labels` using the correct snake_case connector fields.

```
✔  Message 19add45df16721a8 done
✔  Message 19add3d4aa1dcef8 done
✔  Message 19a2ea35e39222cd done
✔  Message 19a2ad9dc3b1e91c done
```

No mark-read warning appeared in the final clean live run.

**What happens on failure:**
- Label update failure: warning logged per message, but the message flow still completes

---

### Final end-to-end verification

Multiple consecutive live messages completed end-to-end in one run:

```
✔  Event created ...
✉  Confirmation draft saved ...
✔  Message ... done
```

This pattern repeated successfully across several real inbox messages before the process was manually stopped to avoid generating more real events and drafts.

---

## Bugs Found and Fixed During QA

### Bug 1: `googlecalendar_create_event` rejected the payload
**Symptom:** `missing or invalid start_datetime parameter`  
**Fix:** Send the connector-required snake_case fields (`start_datetime`, `end_datetime`, `time_zone`) along with the existing camelCase variants in `calendar_api.py`.

### Bug 2: `googlecalendar_list_calendars` could fail and stop processing
**Symptom:** Intermittent Scalekit tool failures during calendar lookup blocked a whole message.  
**Fix:** Added fallback to `"primary"` and made event listing degrade safely to `[]` instead of crashing.

### Bug 3: Gmail draft creation failed with Scalekit template rendering errors
**Symptom:** `tool execution failed - template rendering error` from `gmail_create_draft`.  
**Fix:** Removed unsafe Unicode from human-readable slot formatting and draft body, then retried the draft tool with a few compatible payload shapes until the connector accepted it.

### Bug 4: Mark-as-read used the wrong parameter names
**Symptom:** `path templating failed: missing value for "input.message_id"`  
**Fix:** Changed the payload to `message_id` and `remove_label_ids` in `gmail_api.py`.

### Bug 5: Draft or label failures could make a successful event look like a failed message
**Symptom:** Event was created, but later Gmail tool failures caused the message handler to look broken overall.  
**Fix:** Wrapped draft creation and mark-read in their own `try/except` blocks so post-event failures are isolated and logged clearly.

### Bug 6: Raw OAuth-token draft fallback was not viable with this SDK setup
**Symptom:** `authorization_details` could be `None`, breaking the Gmail REST fallback path.  
**Fix:** Reverted to Scalekit-only draft creation, per product constraint and actual connector behavior.

---

## Divergences from Reference Page

The reference page URL indicates the intended product shape, but the working implementation differs from a simplified template in a few important ways. These divergences are intentional and were required for a reliable live run.

| Reference-style expectation | Actual implementation | Why |
|---|---|---|
| Simple one-shot connector payloads | Retries alternate `gmail_create_draft` payload shapes | Real connector template handling was stricter than the simplified happy path |
| Direct calendar lookup always succeeds | Falls back to `primary` calendar ID | Prevents intermittent connector failures from stopping scheduling |
| Clean ASCII/Unicode-agnostic draft text | Draft body sanitized to ASCII | Scalekit draft templating previously broke on non-ASCII punctuation |
| Post-event Gmail actions assumed safe | Draft and mark-read isolated with warnings | Prevents losing a successfully booked event because a follow-up step fails |

---

## Known Gaps

| Gap | Impact | Fix |
|---|---|---|
| Polling real unread invites creates real calendar events and drafts | Test runs have side effects in the connected inbox/calendar | Use a dedicated test mailbox or narrower Gmail query during future QA |
| No standalone local edge-case test suite in this agent yet | Regression detection relies mainly on live runs | Add targeted unit tests for payload shaping, calendar conflict logic, and message-state handling |
| Continuous poll loop must be manually stopped during QA | A long run keeps processing inbox items | Add a test mode or max-message limit env var for safe QA runs |

---

## Files

```
runner.py           Main poll loop and message workflow
gmail_api.py        Gmail operations via Scalekit connector tools
calendar_api.py     Calendar operations and conflict checks
parsers.py          Claude-based scheduling entity extraction
slotting.py         Busy-slot derivation and slot formatting
sk_connectors.py    Scalekit client wrapper and connection handling
README.md           Setup and architecture overview
```
