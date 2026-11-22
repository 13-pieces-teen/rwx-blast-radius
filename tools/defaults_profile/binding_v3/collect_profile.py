"""Baseline-only default metadata/binding observations; no patch/answer inputs."""
from __future__ import annotations
import argparse, ast, builtins, concurrent.futures, gzip, hashlib, importlib
import importlib.abc, importlib.machinery
import json, os, platform, shutil, sys
from collections import Counter, defaultdict
from pathlib import Path
import pytest
from build_maps import Collector, InlineExecutor
from profile_support import DefaultObserver, Instrument, definitions, GETATTR, REGISTER, READ, instrument_inspect

class NoCoverage:
    def switch_context(self, context): pass

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def snapshot(source):
    return {p.relative_to(source).as_posix():sha(p) for p in sorted(source.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts and p.suffix!='.pyc'}
def write(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,sort_keys=True);f.write('\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source',type=Path,default=Path('/opt/sqlglot'));p.add_argument('--map',type=Path,required=True)
    p.add_argument('--mode',choices=['warm','cold','eager-import-collection'],required=True);args=p.parse_args()
    out=args.output;out.mkdir(parents=True,exist_ok=True)
    if (out/'observations.json.gz').exists():raise FileExistsError(out)
    source=Path('/tmp/default-profile-source');shutil.copytree(args.source,source,ignore=shutil.ignore_patterns('__pycache__','.git'))
    before=snapshot(source);write(out/'before.json',before)
    baseline=json.loads(gzip.decompress(args.map.read_bytes()));texts={};hashes={};known={}
    for relative,expected in baseline['source_hashes'].items():
        text=(source/relative).read_text(encoding='utf-8');value=hashlib.sha256(text.encode()).hexdigest()
        if value!=expected:raise ValueError('baseline source mismatch '+relative)
        texts[relative]=text;hashes[relative]=value
        if relative.startswith('sqlglot/'):
            tree=ast.parse(text);known.update(definitions(relative,tree))
            if any(isinstance(n,ast.Name) and n.id in (REGISTER,READ) for n in ast.walk(tree)):raise ValueError('observer collision')
    os.chdir(source);sys.path.insert(0,str(source));collector=Collector(source,out);collector.cov=NoCoverage();collector.cold_caches=args.mode=='cold'
    observer=DefaultObserver(known,lambda:collector.context)
    errors=[];events=[];starts=[];finishes=[];phases=defaultdict(list);counts=Counter()
    class Recorder:
        def pytest_collectreport(self,report):
            if report.failed:errors.append({'phase':'collection','id':report.nodeid,'detail':report.longreprtext})
        def pytest_runtest_logstart(self,nodeid,location):starts.append(nodeid)
        def pytest_runtest_logfinish(self,nodeid,location):finishes.append(nodeid)
        def pytest_runtest_logreport(self,report):
            kind=type(report).__name__;counts[(kind,report.when,report.outcome)]+=1
            row={'id':report.nodeid,'kind':kind,'phase':report.when,'outcome':report.outcome}
            if kind=='TestReport':phases[report.when].append(report.nodeid)
            if report.failed:row['detail']=report.longreprtext;errors.append(row)
            events.append(row)
    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self,fullname):
            path=Path(self.path);relative=path.relative_to(source).as_posix()
            tree=ast.parse(path.read_text(encoding='utf-8'),filename=self.path)
            return compile(ast.fix_missing_locations(Instrument(relative,known).visit(tree)),self.path,'exec',dont_inherit=True)
        def exec_module(self,module):
            module.__dict__.update({REGISTER:observer.register,READ:observer.read});super().exec_module(module)
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname.split('.')[0] not in ('sqlglot','tests'):return None
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and spec.origin and spec.origin.endswith('.py') and Path(spec.origin).is_relative_to(source):spec.loader=Loader(fullname,spec.origin)
            return spec
    concurrent.futures.ProcessPoolExecutor=InlineExecutor
    sys.meta_path.insert(0,Finder());restore=instrument_inspect(observer);builtins.getattr=observer.read
    sys.addaudithook(observer.audit);sys.setprofile(observer.profile)
    try:
        if args.mode=='eager-import-collection':
            for path in sorted((source/'sqlglot').rglob('*.py')):
                if path.name=='__main__.py':continue
                name=path.relative_to(source).with_suffix('').as_posix().replace('/','.').removesuffix('.__init__')
                try:importlib.import_module(name)
                except Exception as exc:errors.append({'phase':'eager-import','id':name,'detail':repr(exc)})
        argv=['tests','-q','--assert=plain','-p','no:cacheprovider','--timeout=900','--tb=short','-rN']
        if args.mode=='eager-import-collection':argv.insert(0,'--collect-only')
        code=int(pytest.main(argv,plugins=[collector,Recorder()]))
    finally:sys.setprofile(None);builtins.getattr=GETATTR;restore()
    ids=sorted(collector.items);identity=ids==baseline['ids'];lifecycle=True
    if args.mode!='eager-import-collection':
        lifecycle=(starts==collector.items and finishes==collector.items and set(phases['setup'])==set(ids) and set(phases['teardown'])==set(ids)
                   and len(phases['setup'])==len(ids) and len(phases['teardown'])==len(ids)
                   and {r['id'] for r in events if r['kind']=='TestReport' and (r['phase']=='call' or r['phase']=='setup' and r['outcome']=='skipped')}==set(ids))
    after=snapshot(source);write(out/'after.json',after)
    changed={k for k in before if before[k]!=after.get(k)}
    records={}
    for site,r in observer.records.items():
        records[site]={'metadata':sorted(r['metadata']),'opaque':r['opaque'] or site not in observer.functions,'parameters':{}}
        for name,v in r['parameters'].items():
            records[site]['parameters'][name]={key:sorted(v[key]) for key in ('seen','possible','opaque')}
            records[site]['parameters'][name]['counts']=dict(v['counts'])
    result={'complete':code==0 and identity and lifecycle and not errors and not changed,'mode':args.mode,'pytest_exit':code,
            'ids':ids,'source_commit':baseline['source_commit'],'source_hashes':hashes,'definitions':known,'records':records,
            'failures':errors,'identity_matches':identity,'lifecycle_complete':lifecycle,'before_source_unchanged':not changed,
            'changed_input_files':sorted(changed),'reports':dict((str(k),v) for k,v in counts.items()),
            'python':platform.python_version(),'pytest':pytest.__version__,'affinity':sorted(os.sched_getaffinity(0)),
            'execution_model':'InlineExecutor preserving initializer/call/result pickle; not native persistent pool semantics',
            'generator_hashes':{str(Path(p).name):sha(p) for p in (__file__,Path(__file__).with_name('profile_support.py'),'/tools/build_maps.py')},
            'baseline_map_sha256':sha(args.map)}
    (out/'observations.json.gz').write_bytes(gzip.compress(json.dumps(result,sort_keys=True).encode(),mtime=0))
    write(out/'events.json',{'collected':collector.items,'starts':starts,'finishes':finishes,'reports':events})
    print(json.dumps({k:result[k] for k in ('complete','mode','pytest_exit','reports','failures','identity_matches','lifecycle_complete')}),flush=True)
    return 0 if result['complete'] else 1
if __name__=='__main__':raise SystemExit(main())
