T-Travel — Market Insights launcher 1.0
======================================

INSTALL
Extract this package INTO the existing TT-Robopricing folder, merging START_HERE,
automation and tests. Replace the included automation/intelligence/main.py and
automation/pricing_recommendations.py when asked.
Do not create a second nested TT-Robopricing folder.

This package does NOT replace market_insights.py. Keep the accepted v26.2 file.
It does not contain configuration, secrets, CSV inputs or databases.

OPEN
In VS Code, open START_HERE/4_open_market_insights.py and click Run Python File.
Or double-click START_HERE/4_open_market_insights.cmd in Windows Explorer.
Or, from the project root:
    python START_HERE\4_open_market_insights.py

The panel uses Tkinter, included with standard Windows Python installations.
It runs scripts with the same Python interpreter as the launcher. Existing
collector/pricing packages must already be installed in that interpreter.

FIRST USE
1. Check the Sales input shown in the window. Use 'Choose sales CSV' if needed.
   The CSV is read in its existing location; it is not copied into this project.
2. Ensure your existing automation/intelligence/events.db, sources.json and
   inputs/travel_demand files are still present. No blank database is created.
3. Start your existing Ollama/Qwen service if you want AI story extraction.
4. Click Run required steps. The first run records a baseline for freshness;
   existing outputs are labelled Not tracked until run through this launcher.
5. Market Insights opens after the report build, unless you untick that option.

ORDER
1. daily_event_radar.py: RSS stories, destination coverage and dated events.
2. collect_travel_demand.py: configured demand signals and holiday/season inputs.
3. build_opportunities.py: current sales plus route scores; event matching is
   already part of this script, so there is no separate match-events run.
4. pricing_recommendations.py: advisory recommendations from the current price
   book and imported competitor snapshot; no price-book annotations or HTML
   generation during this launcher step.
5. market_insights.py: one final HTML report, using the refreshed evidence.

The launcher's two optional pricing function arguments default to True for old
callers. The launcher passes False for both, avoiding duplicate report work and
leaving the current price book untouched. Standalone/editor behavior is retained.

BUTTONS
Run required steps: refresh missing/outdated/changed steps and dependent outputs.
Run all: run all five steps in order, using each collector's existing cache policy.
Run selected + prerequisites: force that step, first running outdated prerequisites.
  It does NOT automatically run downstream steps. Run required afterwards if needed.
Stop after current step: let the running script finish, then stop the sequence.
  The window cannot close during execution; this avoids interrupting SQLite writes.
Refresh status: recheck recorded runs and local inputs.
Open Market Insights: open the existing report; it does not rebuild it.
Open latest step log: select a process row first.

STATUS
Ready means the step completed without detected source warnings for the recorded
inputs. Collectors, scores and report are due again on a new local calendar day.
Pricing is due when inputs change or after seven days.
Changed scripts, sales, price book, competitor snapshots and relevant DB table
contents invalidate the corresponding steps. The shared SQLite file is not used
as one global timestamp: a scoring write must not invalidate its own inputs.

Warning means the script returned successfully but reported a source problem,
such as failed Trends/RSS access or unavailable Qwen. Downstream steps may use
existing evidence; inspect logs and report coverage/dates. Warning is not Ready.
Hard errors stop the sequence and keep the previous report available.
Last clean success is a tracked process timestamp, not proof that all underlying
business data is recent. Input modification times are labelled explicitly.

LOCAL STATE / GITHUB
Run records, selected sales path and full process logs are saved under:
    outputs/intelligence_launcher/
Keep outputs/ ignored in Git, as in your current repository configuration.
No credentials are stored in launcher settings. Existing Google authentication
and Intelligence configuration remain in their existing locations.
A process lock prevents two instances of this launcher running concurrently.
It does not coordinate scripts that you separately launch outside the panel.

WHAT STAYS SEPARATE
- Import the weekly competitor proposal pack with your existing START_HERE/1 tool.
  The new launcher does not scrape competitors or download a fresh pack.
- Manual Google Ads export/import remains a separate operation. Imported data is
  picked up through the database. No new Ads API/GA4 integration is introduced.
- The Pricing Editor and Promo Planner still manage publishing/executing changes.
- Nothing here applies new prices, executes promos or sends an agency campaign.
- No automatic background schedule is created: run the launcher when required.

OLD INTELLIGENCE MAIN
The included automation/intelligence/main.py is now a small redirect to the same
panel. Existing references continue to work. Neither entry point runs
build_campaign_dashboard.py or creates campaign_opportunity_dashboard.html.

OPTIONAL CLI
    python START_HERE\4_open_market_insights.py --status
    python START_HERE\4_open_market_insights.py --cli
    python START_HERE\4_open_market_insights.py --cli --full
A hard failure or requested stop returns nonzero. Source warnings are recorded
separately even if the process sequence completes.

VERIFICATION
    python tests\test_intelligence_launcher.py
Nine synthetic offline tests cover order, dependencies, freshness, source
warnings, failure/stop behavior, locks, unchanged price-book contents, and the
pricing function's existing default behavior. No real network calls are made.
Python compilation and both launcher entry points were checked. A graphical
window could not be visually tested in the build environment (no display server).
