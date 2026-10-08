T-TRAVEL PROMO PLANNER v0.1 - TEST PACK
=======================================

WHERE TO COPY
-------------
Unzip this package directly into the root of your existing TT-Robopricing folder.
It adds:
  START_HERE\3 Open promo planner.cmd
  automation\promo_planner\...
  outputs\promo_planner\ (runtime state lives here)

It does NOT replace or reorganize your current files.

HOW TO START
------------
Double-click:
  START_HERE\3 Open promo planner.cmd

WHAT v0.1 DOES
--------------
- Reads destinations, plans and all available durations directly from your live
  outputs\manual_prices\manual_prices_current.xlsx.
- Search + multi-select destinations; drag/release or double-click to add.
- Save reusable destination groups.
- Select plans and durations.
- Create percentage or absolute promotions with start/end dates.
- Store planned campaigns in outputs\promo_planner\promo_schedule.db.
- Calendar/execution dashboard shows START due / Active / END due / Completed.
- Preview START/END before changing prices.
- Detect collisions with an existing different PromoCode and BLOCK execution.
- Detect below-cost-floor prices and BLOCK them unless Allow below cost was
  explicitly selected on the campaign.
- Creates a timestamped full workbook backup before every execution.
- START writes the campaign PromoCode to matching rows and adds/updates the
  hidden Promos catalogue in the existing workbook.
- END only removes rows that still carry this campaign's PromoCode, so it will
  not blindly delete a later different promotion.
- Creates Outlook-compatible .ics start/end events and opens them with the
  default Windows calendar app.
- Buttons can open the existing Pricing Editor and Partner Pack launcher when
  their START_HERE filenames contain those words.

OLLAMA
------
Ollama is OPTIONAL. The planner needs no Ollama to create, schedule, preview or
execute promotions.

At startup it checks http://127.0.0.1:11434. If Ollama is absent, the AI button
is simply disabled and everything else keeps working.

If Ollama is installed, v0.1 automatically uses the first locally available
model. No model name is hard-coded. We can add a model selector/settings in the
next iteration after you test it.

SAFETY / FIRST TEST
-------------------
For the first test, create a campaign and use Preview only. Then create a small
campaign for one destination + one duration + one plan and execute START.
Immediately open the Pricing Editor and verify the promo visually.

Every execution backup is stored under:
  outputs\promo_planner\backups\

If Excel has manual_prices_current.xlsx open, Windows may prevent saving. Close
Excel/the workbook before executing a promotion.

DEPENDENCIES
------------
Uses Python standard library + openpyxl, which your pricing project already uses.
No new Ollama, Outlook COM, or UI package is required.
