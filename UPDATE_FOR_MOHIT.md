# Workflow Update — Niche Pivot to Marketing Agencies (PR + Reputation)

**Date:** 2026-06-20
**From:** Rizan
**Re:** Outreach pipeline repositioned from HVAC → US marketing/social agencies

---

Hi Mohit,

Here's a rundown of the changes we just shipped to the lead pipeline based on our decision to
go after **social-media & digital-marketing agencies in the US**, offering **guaranteed PR
placements + reputation management** (removing policy-violating Google reviews and suppressing
negative URLs) instead of the old HVAC review-removal angle.

## What changed in the workflow

1. **All email copy rewritten** to the new agency/PR offer, based on the Digital Nod sequence:
   - **Touch 1** — cold opener ("77% of your next client already checked your reviews").
   - **Touch 2** — same-thread follow-up referencing their Google Business Profile.
   - **Touch 3** — same-thread value nudge (no invented case-study numbers yet).
   - **Touch 4 (NEW)** — a soft breakup ("closing the loop").
   - Touches 2-4 thread off Touch 1 with a `Re:` subject so it reads like one conversation.

2. **Sequence extended from 3 → 4 touches.** The number of touches is now controlled entirely by
   one setting (`MAX_FOLLOWUPS`), with no hardcoded limits anywhere in the code. If we ever want a
   5th touch later, it's just: add one template file + bump that number. Nothing else to touch.

3. **Social templates (LinkedIn + Facebook) rewritten** to match the new offer, in case we run
   them through PhantomBuster.

4. **Targeting profile (ICP) updated** in the project docs to agencies (persona, company size,
   industries, region = USA).

5. **Lead database reset for testing.** I wiped the old HVAC leads out of the Google Sheet (backed
   them up first) and left a single test lead — me (Rizan) — so the next outreach run sends the
   brand-new sequence to my own inbox. That lets us QA the emails end-to-end before pointing it at
   real agencies.

## What I need from you (action items)

These live in GitHub settings, which I can't change on your behalf:

1. **Set the GitHub repo variables** (Settings → Secrets and variables → Actions → *Variables*):
   - `MAX_FOLLOWUPS` = `4`
   - `ICP_PERSONA` = `agency owner,founder,CEO,managing director,partner,head of growth`
   - `ICP_COMPANY_SIZE` = `2-10,10-50`
   - `ICP_INDUSTRIES` = `Marketing & Advertising,Marketing Services,Advertising Services,Digital Marketing,Social Media Marketing,Public Relations & Communications`
   - `ICP_REGIONS` = `USA`
   - `ICP_DISQUALIFY` = `Not a marketing/advertising/digital/social-media agency; in-house marketing teams; companies outside the US`

   > Until these are set, lead **discovery** keeps using the old HVAC targeting. The outreach copy
   > is already live regardless.

2. **Add a compliance footer** to the sending account's signature (CAN-SPAM): business physical
   address, a one-line "this is a promotional message from [Agency]" note, and a working
   unsubscribe link.

3. **Review & merge the PR** I've opened so these changes land on the main branch.

## Heads-up: the schedules are paused

The Phase 1, Phase 2, and reply-logger GitHub Actions schedules are currently **paused**
(commented out) while we finish the makeover — so nothing auto-sends yet. To run the QA test
against my test lead, either:
- **Trigger it manually** — GitHub → Actions → `phase2-outreach` → "Run workflow" (this works even
  while the schedule is paused), after setting `MAX_FOLLOWUPS=4`; or
- **Re-enable the schedules** by uncommenting the `schedule:` blocks in the three workflow files
  (they only re-register once merged to `main`).

## Notes

- The old HVAC leads are safely backed up locally before the wipe — nothing is lost; just say the
  word if you want them.
- Once the repo variables are set and a run is triggered, it will (a) send my test sequence and
  (b) start discovering agency leads. I'd suggest we watch that first run together.

Thanks,
Rizan
