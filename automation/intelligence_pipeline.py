"""Local advisory pipeline. No scrapes, price publication or campaign execution.

Run records and settings live in outputs/intelligence_launcher. Opening the
launcher never creates or mutates events.db. Child jobs use explicit paths.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time
import errno
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

VERSION = '1.0.4-monthly-planning-inputs'
ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parsed_time(value):
    try:
        return datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return None


def atomic_json(path, data):
    """Replace state atomically, retrying temporary Windows/OneDrive file locks.

    Keep the old JSON intact on failure. Never unlink the destination or fall
    back to truncating it in place. Close our temp handle before os.replace.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=path.parent, prefix=path.stem+'-', suffix='.tmp', delete=False) as handle:
            temp = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        delays = (0, 0.15, 0.3, 0.6, 1.0, 1.5, 2.0, 3.0)
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                os.replace(temp, path)
                return
            except OSError as exc:
                locked = (isinstance(exc, PermissionError)
                          or getattr(exc, 'winerror', None) in (5, 32, 33)
                          or exc.errno in (errno.EACCES, errno.EBUSY, errno.EPERM))
                if not locked:
                    raise
                if attempt == len(delays)-1:
                    raise PermissionError(
                        f'Cannot save launcher status/settings: {path}. '
                        'Windows still denies replacement after several retries. '
                        'The previous file was preserved. Close any program holding '
                        'the file; if OneDrive is locking it, pause syncing briefly '
                        'and retry. A persistent error may require checking folder permissions.'
                    ) from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # A locked temporary file must not hide the original error.


def literal_config(path, field):
    """Read only literal public path settings; don't execute credential config."""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == field for t in node.targets):
                value = ast.literal_eval(node.value)
                return value if isinstance(value, str) else ''
    except (OSError, ValueError, SyntaxError):
        pass
    return ''


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    script: str
    dependencies: tuple[str, ...] = ()
    daily: bool = True


STEPS = (
    Step('radar', '1 · Travel stories and events', 'daily_event_radar.py'),
    Step('demand', '2 · Travel demand signals', 'collect_travel_demand.py'),
    Step('scores', '3 · Sales and route opportunities', 'build_opportunities.py', ('radar', 'demand')),
    Step('pricing', '4 · Pricing recommendations', 'pricing_recommendations.py', (), False),
    Step('report', '5 · Market Insights', 'market_insights.py', ('scores', 'pricing', 'radar')),
)
BY_KEY = {s.key: s for s in STEPS}
CURRENT_STATES = {'Ready', 'Warning', 'Ready*'}


class Pipeline:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.automation = self.root / 'automation'
        self.intelligence = self.automation / 'intelligence'
        self.local = self.root / 'outputs' / 'intelligence_launcher'
        self.state_path = self.local / 'runs.json'
        self.settings_path = self.local / 'settings.json'
        self.settings = self.load_json(self.settings_path)
        self.state = self.load_json(self.state_path)
        self.stop = threading.Event()
        self._db_cache = {}

    @staticmethod
    def load_json(path):
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            return data
        except (ValueError, OSError) as exc:
            raise RuntimeError(f'Cannot read launcher state/settings at {path}: {exc}') from exc

    @property
    def database(self):
        return self.intelligence / 'events.db'

    @property
    def report(self):
        return self.root / 'outputs/pricing_recommendations/market_insights_latest.html'

    @property
    def recommendations(self):
        return self.root / 'outputs/pricing_recommendations/recommendations_latest.csv'

    @property
    def market(self):
        return self.root / 'outputs/market_analysis/market_prices_annotated_latest.csv'

    @property
    def price_book(self):
        folder = self.root / 'outputs/manual_prices/current'
        xlsx = folder / 'manual_prices_current.xlsx'
        return xlsx if xlsx.exists() else folder / 'manual_prices_current.csv'

    @property
    def sales(self):
        if (self.root / 'outputs' / '.development_workspace').exists():
            # Ignore inherited settings and environment overrides in the dev copy.
            return self.root / 'local_data' / 'T-Travel_sales_master.csv'
        value = self.settings.get('sales_file') or os.environ.get('TT_INTELLIGENCE_SALES_FILE') or os.environ.get('TTRAVEL_SALES_MASTER_CSV')
        if not value:
            value = literal_config(self.intelligence / 'config.py', 'SALES_FILE')
        default = Path.home() / 'Deutsche Telekom AG/HT-TD Travel eSIM Launch - Documents/General/11_Reporting/T-Travel_sales_master.csv'
        path = Path(value).expanduser() if value else default
        return path if path.is_absolute() else self.intelligence / path

    def save_settings(self, sales_file):
        self.settings['sales_file'] = str(sales_file).strip()
        atomic_json(self.settings_path, self.settings)

    def script(self, key):
        step = BY_KEY[key]
        return (self.intelligence if key in ('radar', 'demand', 'scores') else self.automation) / step.script

    @staticmethod
    def file_stamp(path):
        path = Path(path)
        try:
            stat = path.stat()
            return [str(path), stat.st_size, stat.st_mtime_ns]
        except OSError:
            return [str(path), 'missing']

    def table_digest(self, tables, exclude_scoring_sales=False):
        """Hash only dependency tables, not the whole shared SQLite file.

        A scoring write must not make its own demand input look outdated.
        Cached for the current DB/WAL file stamps; no article content is logged.
        """
        if not self.database.exists():
            return 'missing database'
        stamp = json.dumps([self.file_stamp(self.database), self.file_stamp(str(self.database)+'-wal'), list(tables), exclude_scoring_sales])
        if stamp in self._db_cache:
            return self._db_cache[stamp]
        digest = hashlib.sha256()
        try:
            with sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=2) as db:
                db.execute('BEGIN')
                for table in tables:
                    digest.update(table.encode())
                    if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
                        digest.update(b'missing table'); continue
                    # Fixed internal table names, never user SQL.
                    condition = " WHERE signal_type != 'observed_sales_60d' OR signal_type IS NULL" if table == 'demand_observations' and exclude_scoring_sales else ''
                    for row in db.execute(f'SELECT * FROM "{table}"{condition} ORDER BY rowid'):
                        digest.update(json.dumps(row, ensure_ascii=True, default=str).encode())
        except sqlite3.Error as exc:
            return 'database unreadable: '+type(exc).__name__
        result = digest.hexdigest()
        if len(self._db_cache) > 30:
            self._db_cache.clear()
        self._db_cache[stamp] = result
        return result

    def signature(self, key):
        files = [self.script(key), Path(__file__), self.intelligence/'route_policy.py']
        data = {}
        config_files = [self.intelligence/'config.py', self.automation/'config.py']
        input_files = list((self.intelligence/'inputs/travel_demand').glob('*.csv'))
        if key == 'radar':
            files += [self.intelligence/'sources.json', self.intelligence/'destination_coverage.py', self.intelligence/'event_matching.py']
        if key in ('demand','scores','report'):
            files += config_files + input_files
        if key == 'demand':
            files += [self.root/'inputs/WS_PPG.csv', self.intelligence/'collector_cache.py']
        if key == 'scores':
            files += [self.sales, self.intelligence/'event_matching.py']
            data['tables'] = self.table_digest(('demand_observations','events'), exclude_scoring_sales=True)
        if key == 'pricing':
            files += [self.price_book, self.market, self.root/'inputs/destinations.yaml', self.root/'inputs/promos.json', self.automation/'market_history.py', self.automation/'currency_support.py']
        if key == 'pricing':
            files += list((self.root/'outputs/combined_scrapes/history').glob('*.csv'))
        if key == 'report':
            files += [self.automation/'campaign_monthly.py', self.sales, self.price_book, self.recommendations, self.market, self.automation/'market_history.py', self.automation/'pipeline_files.py', self.intelligence/'destination_coverage.py', self.intelligence/'event_matching.py', self.root/'inputs/destinations.yaml', self.root/'inputs/promos.json']
            files += list((self.root/'outputs/combined_scrapes/history').glob('*.csv'))
            files += list((self.root/'outputs/manual_prices/history').glob('**/manual_prices_*'))
            data['tables'] = self.table_digest(('opportunity_scores','events','destination_articles','destination_article_topics','google_ads_keywords','holiday_periods','collector_refresh'))
        data['files'] = [self.file_stamp(f) for f in sorted(set(files)) if not f.is_dir()]
        data['dependencies'] = {dep:self.state.get(dep,{}).get('last_completed') or self.state.get(dep,{}).get('finished_at','') for dep in BY_KEY[key].dependencies}
        return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

    def table_summary(self, table, date_column=None):
        if not self.database.exists():
            return None, None
        try:
            with sqlite3.connect(self.database.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                    return None,None
                tail = f', MAX("{date_column}")' if date_column else ', NULL'
                return db.execute(f'SELECT COUNT(*){tail} FROM "{table}"').fetchone()
        except sqlite3.Error:
            return None,None

    def outputs_exist(self, key):
        if key == 'radar':
            return self.table_summary('events')[0] is not None
        if key == 'demand':
            return self.table_summary('demand_observations')[0] is not None
        if key == 'scores':
            return self.table_summary('opportunity_scores')[0] is not None
        return (self.recommendations if key=='pricing' else self.report).exists()

    def status(self, key):
        if not self.script(key).exists():
            return 'Blocked', 'Script missing'
        entry = self.state.get(key,{})
        if not entry:
            return 'Not tracked', 'Existing output detected; run once to record freshness' if self.outputs_exist(key) else 'No recorded run'
        if entry.get('status') in ('failed','running','stopped'):
            return 'Needs run', 'Previous run failed/interrupted' if entry.get('status')!='running' else 'Previous run interrupted or still running'
        if not self.outputs_exist(key):
            return 'Needs run', 'Output missing'
        success = parsed_time(entry.get('last_completed') or entry.get('finished_at') or entry.get('last_success'))
        if not success:
            return 'Needs run', 'No recorded completed run'
        now=datetime.now().astimezone()
        if BY_KEY[key].daily and success.date()!=now.date():
            return 'Needs run', 'Daily refresh due'
        if not BY_KEY[key].daily and (now-success).total_seconds()>7*86400:
            return 'Needs run', 'Weekly refresh due'
        if self.signature(key)!=entry.get('signature'):
            return 'Needs run', 'Inputs, code or upstream results changed'
        if entry.get('status')=='warning':
            count=entry.get('warning_count',0)
            categories=', '.join(entry.get('warning_categories',{}))
            return 'Warning', f'Completed; {count} source warning(s)'+(': '+categories if categories else '; see log')
        return 'Ready', 'Current for recorded inputs'

    def statuses(self):
        result={}
        for step in STEPS:
            status,reason=self.status(step.key)
            waiting=[BY_KEY[d].title.split(' · ')[-1] for d in step.dependencies if result.get(d,('Ready',))[0] not in CURRENT_STATES]
            limited=[BY_KEY[d].title.split(' · ')[-1] for d in step.dependencies if result.get(d,('Ready',))[0] in ('Warning','Ready*')]
            if waiting and status in CURRENT_STATES:
                status,reason='Needs run','Upstream refresh needed: '+', '.join(waiting)
            elif limited and status=='Ready':
                status,reason='Ready*','Completed using available evidence; source warnings upstream: '+', '.join(limited)
            result[step.key]=(status,reason)
        return result

    def plan(self, mode='needed', selected=None):
        statuses=self.statuses()
        chosen=set()
        def add(key, force=False):
            for dep in BY_KEY[key].dependencies:
                add(dep)
            if force or statuses[key][0] not in CURRENT_STATES or any(d in chosen for d in BY_KEY[key].dependencies):
                chosen.add(key)
        if mode=='selected':
            if selected not in BY_KEY: raise ValueError('Select a step')
            add(selected,True)
        else:
            for step in STEPS: add(step.key,mode=='full')
        return [s.key for s in STEPS if s.key in chosen]

    def preflight(self, key):
        required=[self.script(key)]
        if key=='radar':
            required += [self.database,self.intelligence/'sources.json']
        if key=='demand':
            required += [self.root/'inputs/WS_PPG.csv']
            required += [self.intelligence/'inputs/travel_demand'/f for f in ('origins.csv','destination_aliases.csv','origin_keyword_templates.csv','destination_seasonality.csv','origin_market_weights.csv')]
        if key in ('scores','report'): required += [self.database,self.sales,self.intelligence/'inputs/travel_demand/origin_market_weights.csv']
        if key in ('pricing','report'): required += [self.price_book,self.market]
        if key=='report': required += [self.recommendations]
        missing=[str(x) for x in required if not x.exists()]
        if missing: raise RuntimeError('Missing required input(s):\n'+'\n'.join(missing))
        if key=='radar' and self.table_summary('events')[0] is None:
            raise RuntimeError('The selected events.db has no events table. Restore the existing Intelligence database; this launcher does not create a replacement.')

    def command(self,key):
        return [sys.executable,'-u',str(Path(__file__).resolve()),'--worker',key,'--root',str(self.root)]

    def save_record(self,key,**fields):
        # Do not claim a saved status in the UI if writing it failed.
        updated = dict(self.state)
        updated[key] = dict(self.state.get(key, {}), **fields)
        atomic_json(self.state_path, updated)
        self.state = updated

    def execute(self, keys, emit:Callable[[str,str],None]=lambda kind,line:print(line)):
        """Synchronous engine; UI runs it in a thread. Stop is between jobs only."""
        self.stop.clear()
        self.local.mkdir(parents=True,exist_ok=True)
        lock_path=self.local/'pipeline.lock'
        # OS advisory lock releases after a crash; stale lock files are harmless.
        lock=lock_path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            lock.close();emit('error','Another launcher run is active.');return False
        try:
            self.state=self.load_json(self.state_path)
            # Retention runs before jobs so reports and recorded signatures use
            # the same retained history. Hold the existing launcher lock.
            try:
                from intelligence_maintenance import clean_database
                maintenance = clean_database(
                    self.database, self.local/'retention_backup.db')
                atomic_json(self.local/'maintenance_latest.json', maintenance)
                removed = sum(maintenance.get('deleted', {}).values())
                emit('line', f'MAINTENANCE: {maintenance["status"]}; {removed} expired rows removed. Details: maintenance_latest.json')
            except Exception as exc:
                emit('error', f'Database maintenance failed; pipeline stopped: {exc}')
                return False
            for index,key in enumerate(keys,1):
                if self.stop.is_set():
                    emit('stopped','Stopped between steps. Completed work was saved.');return False
                emit('step',key)
                emit('line',f'[{index}/{len(keys)}] {BY_KEY[key].title}')
                stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                log_path=self.local/f'{stamp}-{key}.log'
                self.save_record(key,status='running',started_at=utc_now(),log=str(log_path))
                try:
                    self.preflight(key)
                    # Capture input identity before execution to detect changes made mid-run.
                    signature=self.signature(key)
                    warnings=0
                    warning_categories={}
                    env=os.environ.copy();env['PYTHONUTF8']='1';env['PYTHONIOENCODING']='utf-8';env['PYTHONUNBUFFERED']='1'
                    with log_path.open('w',encoding='utf-8') as log:
                        process=subprocess.Popen(self.command(key),cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',env=env)
                        assert process.stdout is not None
                        for line in process.stdout:
                            log.write(line);log.flush();emit('line',line.rstrip())
                            warning_match=re.search(r'^(AI ERR|ERR\b|HOLIDAY ERR|GOOGLE TRENDS (ERR|SKIP)|GOOGLE ADS ERR|SALES (ERR|SKIP)|ORIGIN FILTER SKIP)|Qwen: unavailable',line)
                            if warning_match:
                                warnings+=1
                                category=warning_match.group(0)
                                warning_categories[category]=warning_categories.get(category,0)+1
                        process.stdout.close()
                        code=process.wait()
                    if code: raise RuntimeError(f'Process exited with code {code}. See the step log.')
                    if not self.outputs_exist(key): raise RuntimeError('Process exited successfully but the expected output is missing.')
                    if key in ('demand','scores'):
                        table,column=('demand_observations','observed_date') if key=='demand' else ('opportunity_scores','score_date')
                        if self.table_summary(table,column)[1]!=datetime.now().date().isoformat():
                            warnings+=1
                            warning_categories['No current-day observations']=1
                    fields=dict(status='warning' if warnings else 'success',finished_at=utc_now(),signature=signature,warning_count=warnings,warning_categories=warning_categories)
                    fields['last_completed']=fields['finished_at']
                    if not warnings: fields['last_success']=fields['finished_at']
                    self.save_record(key,**fields)
                    emit('line',f'Completed {key}'+(f' with {warnings} source warning(s). Existing evidence may have been reused.' if warnings else '.'))
                except Exception as exc:
                    with log_path.open('a',encoding='utf-8') as log: log.write('\nLAUNCHER ERROR: '+str(exc)+'\n')
                    self.save_record(key,status='failed',finished_at=utc_now(),error=str(exc))
                    emit('error',str(exc));return False
                emit('updated',key)
            emit('done','Run completed. Review any source warnings before using recommendations.')
            return True
        finally:
            lock.close()

    def input_summary(self):
        lines=[]
        for label,path in [('Sales export',self.sales),('Competitor snapshot',self.market),('Current price book',self.price_book),('Events database',self.database)]:
            if path.exists():
                when=datetime.fromtimestamp(path.stat().st_mtime).strftime('%d %b %Y %H:%M')
                lines.append(f'{label}: file modified {when}')
            else: lines.append(f'{label}: MISSING')
        for label,table,column in [('Demand observations','demand_observations','observed_date'),('Route scores','opportunity_scores','score_date')]:
            count,latest=self.table_summary(table,column)
            lines.append(f'{label}: {count if count is not None else "missing"} rows; latest observation {latest or "unavailable"}')
        lines.append('Sales input: '+str(self.sales))
        lines.append('File modification times do not prove source-data freshness. Manual Ads imports remain separate; this launcher does not grant API access.')
        return '\n'.join(lines)


def worker(pipeline, key):
    """Child process entry; keep module search paths and sales input consistent."""
    p=pipeline
    os.environ['TT_INTELLIGENCE_DATABASE']=str(p.database)
    os.environ['TT_INTELLIGENCE_SALES_FILE']=str(p.sales)
    if key in ('radar','demand','scores'):
        # Intelligence config.py must win over automation/config.py.
        sys.path.insert(0,str(p.intelligence))
        os.chdir(p.intelligence)
        import importlib
        module=importlib.import_module(Path(BY_KEY[key].script).stem)
        args=[str(p.script(key)),'--database',str(p.database)]
        if key=='demand': args += ['--input-dir',str(p.intelligence/'inputs/travel_demand'),'--portfolio-file',str(p.root/'inputs/WS_PPG.csv')]
        if key=='scores': module.SALES_FILE=str(p.sales)
        sys.argv=args
        return module.main() or 0
    os.chdir(p.root)
    sys.path.insert(0,str(p.automation))
    from pipeline_files import PipelineFiles
    paths=PipelineFiles(base_dir=p.root)
    if key=='pricing':
        from pricing_recommendations import generate_recommendations_from_files
        generate_recommendations_from_files(paths,generate_insights=False,annotate_workbook=False)
    else:
        from market_insights import generate_market_insights
        generate_market_insights(paths,intelligence_database_path=p.database)
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description='T-Travel advisory pipeline')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--worker',choices=BY_KEY)
    parser.add_argument('--cli',action='store_true')
    parser.add_argument('--full',action='store_true')
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args(argv)
    pipeline=Pipeline(args.root)
    if args.worker: return worker(pipeline,args.worker)
    if args.status:
        for key,(state,why) in pipeline.statuses().items(): print(f'{BY_KEY[key].title}: {state} — {why}')
        print(pipeline.input_summary());return 0
    if args.cli:
        plan=pipeline.plan('full' if args.full else 'needed')
        if not plan: print('Everything is current for the recorded inputs.');return 0
        return 0 if pipeline.execute(plan) else 1
    from intelligence_launcher import launch
    launch(pipeline)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
