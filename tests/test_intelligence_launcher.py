"""Offline synthetic pipeline tests: never use company data or network calls."""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

AUTOMATION=Path(__file__).resolve().parents[1]/'automation'
sys.path.insert(0,str(AUTOMATION))
from intelligence_pipeline import Pipeline, STEPS, utc_now


def put(path,text='fixture'):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='tt-launcher-test-')
        self.root=Path(self.temp.name)
        a=self.root/'automation';i=a/'intelligence'
        i.mkdir(parents=True)
        self.pipeline=Pipeline(self.root)
        put(i/'sources.json','[]')
        sales=self.root/'sales.csv';put(sales,'TransactionDate,Quantity\n2026-10-01,5\n')
        self.pipeline.save_settings(str(sales))
        for name in ('origins.csv','destination_aliases.csv','origin_keyword_templates.csv','destination_seasonality.csv','origin_market_weights.csv'):
            put(i/'inputs/travel_demand'/name,'x\n')
        put(self.root/'inputs/WS_PPG.csv','ISO_Code_A2,country\nTR,Turkey\n')
        put(self.pipeline.price_book,'CurrentNetPrice\n12\n')
        put(self.pipeline.market,'price\n15\n')
        with sqlite3.connect(self.pipeline.database) as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,title TEXT)')
        # Scripts assert real execution paths; no network and no external files.
        common='''from pathlib import Path
import sqlite3,sys
from datetime import date
'''
        put(i/'daily_event_radar.py',common+'''
def main():
    dbpath=Path(sys.argv[sys.argv.index('--database')+1])
    assert dbpath.is_absolute() and Path.cwd()==dbpath.parent
    with sqlite3.connect(dbpath) as db: db.execute("INSERT INTO events(title) VALUES('Synthetic story')")
    print('RADAR fixture completed')
''')
        put(i/'collect_travel_demand.py',common+'''
def main():
    dbpath=Path(sys.argv[sys.argv.index('--database')+1])
    assert Path(sys.argv[sys.argv.index('--portfolio-file')+1]).is_absolute()
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE IF NOT EXISTS demand_observations(observed_date TEXT,signal_type TEXT,normalized_score REAL)')
        db.execute("INSERT INTO demand_observations VALUES(?, 'search_momentum',70)",(date.today().isoformat(),))
    print('GOOGLE ADS SKIP: manual import remains available')
''')
        put(i/'build_opportunities.py',common+'''
SALES_FILE=None
def main():
    assert SALES_FILE and Path(SALES_FILE).exists()
    with sqlite3.connect(sys.argv[sys.argv.index('--database')+1]) as db:
        db.execute('CREATE TABLE IF NOT EXISTS opportunity_scores(score_date TEXT,opportunity_score REAL)')
        db.execute('INSERT INTO opportunity_scores VALUES(?,70)',(date.today().isoformat(),))
        db.execute("INSERT INTO demand_observations VALUES(?, 'observed_sales_60d',60)",(date.today().isoformat(),))
    print('SCORES fixture completed')
''')
        put(a/'pipeline_files.py','''from pathlib import Path
class PipelineFiles:
    def __init__(self,base_dir): self.base_dir=Path(base_dir)
''')
        put(a/'pricing_recommendations.py','''def generate_recommendations_from_files(paths,generate_insights=True,annotate_workbook=True):
    assert generate_insights is False and annotate_workbook is False
    out=paths.base_dir/'outputs/pricing_recommendations/recommendations_latest.csv'
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text('ISO,Plan\\nTR,Unlimited\\n')
    print('PRICING fixture completed')
''')
        put(a/'market_insights.py','''def generate_market_insights(paths,intelligence_database_path=None):
    assert intelligence_database_path.exists()
    (paths.base_dir/'outputs/pricing_recommendations/market_insights_latest.html').write_text('<html>Synthetic report</html>')
    print('REPORT fixture completed')
''')
        self.messages=[]

    def tearDown(self):self.temp.cleanup()

    def run_jobs(self,keys):
        return self.pipeline.execute(keys,lambda kind,line:self.messages.append((kind,line)))

    def test_first_run_order_outputs_and_no_self_invalidation(self):
        before=self.pipeline.price_book.read_bytes()
        keys=self.pipeline.plan()
        self.assertEqual(keys,['radar','demand','scores','pricing','report'])
        self.assertTrue(self.run_jobs(keys),self.messages)
        self.assertEqual(self.pipeline.price_book.read_bytes(),before)
        self.assertTrue(self.pipeline.report.exists())
        self.assertEqual(self.pipeline.plan(),[],self.pipeline.statuses())
        self.assertFalse((self.root/'events.db').exists())
        self.assertFalse((self.pipeline.intelligence/'campaign_opportunity_dashboard.html').exists())
        self.assertEqual([v for k,v in self.messages if k=='step'],keys)
        # Sales observation written by scoring must not dirty its own dependency.
        with sqlite3.connect(self.pipeline.database) as db:
            db.execute("UPDATE demand_observations SET normalized_score=61 WHERE signal_type='observed_sales_60d'")
        self.assertEqual(self.pipeline.status('scores')[0],'Ready')
        with sqlite3.connect(self.pipeline.database) as db:
            db.execute("UPDATE demand_observations SET normalized_score=72 WHERE signal_type='search_momentum'")
        self.assertEqual(self.pipeline.plan(),['scores','report'])

    def test_changed_sales_and_manual_price_book(self):
        self.assertTrue(self.run_jobs(self.pipeline.plan()))
        put(self.pipeline.sales,'TransactionDate,Quantity\n2026-10-02,55\n')
        self.assertEqual(self.pipeline.plan(),['scores','report'])
        self.assertTrue(self.run_jobs(self.pipeline.plan()))
        put(self.pipeline.price_book,'CurrentNetPrice\n13\n')
        self.assertEqual(self.pipeline.plan(),['pricing','report'])

    def test_failure_stops_downstream_and_cli_status_records_failure(self):
        put(self.pipeline.script('demand'),'def main():\n    print("synthetic failure")\n    return 4\n')
        self.assertFalse(self.run_jobs(self.pipeline.plan()))
        self.assertEqual(self.pipeline.state['demand']['status'],'failed')
        self.assertNotIn('report',self.pipeline.state)
        self.assertFalse(self.pipeline.report.exists())
        self.assertTrue(Path(self.pipeline.state['demand']['log']).exists())

    def test_soft_errors_are_warnings_not_clean_success(self):
        with self.pipeline.script('demand').open('a') as f:f.write('\n_original=main\ndef main():\n    _original()\n    print("GOOGLE TRENDS ERR: fixture failure")\n')
        self.assertTrue(self.run_jobs(self.pipeline.plan()))
        self.assertEqual(self.pipeline.state['demand']['status'],'warning')
        self.assertNotIn('last_success',self.pipeline.state['demand'])
        self.assertIn('demand',self.pipeline.plan())
        self.assertEqual(self.pipeline.statuses()['report'][0],'Needs run')

    def test_selected_report_adds_only_required_prerequisites(self):
        self.assertEqual(self.pipeline.plan('selected','report'),[s.key for s in STEPS])
        self.assertTrue(self.run_jobs(self.pipeline.plan()))
        self.assertEqual(self.pipeline.plan('selected','report'),['report'])
        self.assertEqual(self.pipeline.plan('selected','scores'),['scores'])

    def test_stop_between_jobs(self):
        def emit(kind,line):
            if kind=='updated':self.pipeline.stop.set()
        self.assertFalse(self.pipeline.execute(self.pipeline.plan(),emit))
        self.assertEqual(self.pipeline.state['radar']['status'],'success')
        self.assertNotIn('demand',self.pipeline.state)

    def test_daily_expiry_and_legacy_missing_database(self):
        self.assertTrue(self.run_jobs(self.pipeline.plan()))
        for step in STEPS:self.pipeline.state[step.key]['last_success']='2020-01-01T10:00:00+00:00'
        self.assertEqual(self.pipeline.plan(),[s.key for s in STEPS])
        self.pipeline.database.unlink()
        self.pipeline.input_summary()
        self.assertFalse(self.pipeline.database.exists())
        with self.assertRaises(RuntimeError):self.pipeline.preflight('radar')
        self.assertFalse(self.pipeline.database.exists())

    def test_two_launchers_cannot_run_concurrently(self):
        if os.name=='nt':self.skipTest('POSIX lock fixture; Windows uses msvcrt')
        import fcntl
        self.pipeline.local.mkdir(parents=True,exist_ok=True)
        with (self.pipeline.local/'pipeline.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertFalse(self.run_jobs(['radar']))
        self.assertTrue(self.run_jobs(['radar']))


class PricingSafetyTest(unittest.TestCase):
    def test_advisory_options_do_not_annotate_or_double_build(self):
        from unittest.mock import patch
        from contextlib import ExitStack, redirect_stdout
        import io
        import pandas as pd
        import pricing_recommendations as recommendations
        import market_insights
        from pipeline_files import PipelineFiles
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp);book=root/'book.xlsx';market=root/'market.csv'
            put(book,'unchanged synthetic workbook');put(market,'price\n10\n')
            stack.enter_context(patch.object(recommendations,'read_pricing_workbook',return_value=pd.DataFrame()))
            stack.enter_context(patch.object(recommendations,'_exclude_temporal_market_anomalies',return_value=(pd.DataFrame(),0)))
            stack.enter_context(patch.object(recommendations,'_load_promos',return_value={}))
            stack.enter_context(patch.object(recommendations,'_load_priority_countries',return_value=[]))
            stack.enter_context(patch.object(recommendations,'generate_recommendations',return_value=pd.DataFrame({'ISO':['TR']})))
            stack.enter_context(patch.object(recommendations,'build_summary',return_value=pd.DataFrame()))
            annotate=stack.enter_context(patch.object(recommendations,'annotate_price_book_with_recommendations'))
            report=stack.enter_context(patch.object(market_insights,'generate_market_insights'))
            args=dict(paths=PipelineFiles(base_dir=root),price_book_path=book,market_path=market,eur_to_usd=1.1)
            with redirect_stdout(io.StringIO()):
                recommendations.generate_recommendations_from_files(**args,generate_insights=False,annotate_workbook=False)
            annotate.assert_not_called();report.assert_not_called()
            self.assertEqual(book.read_text(),'unchanged synthetic workbook')
            # Existing non-launcher callers retain their original defaults.
            with redirect_stdout(io.StringIO()):recommendations.generate_recommendations_from_files(**args)
            annotate.assert_called_once();report.assert_called_once()


if __name__=='__main__':unittest.main()
