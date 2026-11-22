"""Real tiny execution regressions for metadata, argument binding and escapes."""
import ast
import unittest
import builtins
import difflib
import gzip
import hashlib
import importlib.util
import inspect
import operator
import json
from pathlib import Path
import platform
import sys
import tempfile
import types

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(ROOT),str(HERE)]
from default_dependencies import DefaultsMixin
from selector import Selector
from tools.defaults_profile.identity_v1.profile_support import DefaultObserver,Instrument,definitions,potential_writer,GETATTR,REGISTER,READ,instrument_inspect
class Candidate(DefaultsMixin,Selector):pass

def execute(source,path,actions,fixture=None,observe=False):
    known=definitions('module.py',ast.parse(source));context='@collection';module=types.ModuleType('module')
    observer=DefaultObserver(known,lambda:context);observed={};failures=[]
    module.__dict__.update({REGISTER:observer.register,READ:observer.read})
    tree=ast.fix_missing_locations(Instrument('module.py',known).visit(ast.parse(source))) if observe else ast.parse(source)
    previous_trace=sys.gettrace();previous_profile=sys.getprofile()
    def trace(frame,event,arg):
        if event=='line' and frame.f_code.co_filename==str(path):observed.setdefault(frame.f_lineno,set()).add(context)
        return trace
    # This hook cannot be removed, so only its active lifetime may observe.
    active=[observe]
    def audit(event,args):
        if active[0]:observer.audit(event,args)
    restore_inspect=instrument_inspect(observer) if observe else lambda:None
    if observe:sys.addaudithook(audit);sys.setprofile(observer.profile);builtins.getattr=observer.read
    sys.settrace(trace)
    try:
        exec(compile(tree,str(path),'exec'),module.__dict__)
        env={'m':module,'inspect':inspect,'types':types,'operator':operator,REGISTER:observer.register,READ:observer.read}
        if fixture:
            context='@class:C';ft=ast.fix_missing_locations(Instrument('tests/toy.py',{}).visit(ast.parse(fixture))) if observe else ast.parse(fixture)
            exec(compile(ft,'tests/toy.py','exec'),env)
        for index,action in enumerate(actions):
            context=index;tree=ast.fix_missing_locations(Instrument('tests/toy.py',{}).visit(ast.parse(action))) if observe else ast.parse(action)
            try:exec(compile(tree,'tests/toy.py','exec'),dict(env))
            except AssertionError:failures.append(index)
        return observed,failures,observer
    finally:
        active[0]=False;builtins.getattr=GETATTR;sys.setprofile(previous_profile);sys.settrace(previous_trace);restore_inspect()

def run_cases(output):
    if output.exists():raise FileExistsError(output)
    regular='def f(x=DEFAULT):\n    return x\n'
    cases=[
      ('ordinary',regular,['assert m.f() is True','assert True'],None,[0]),
      ('opposite_bound',regular,['assert m.f(False) is False','assert m.f() is True'],None,[1]),
      ('all_opposite_with_persistent_function','SHARED=None\ndef f(x=DEFAULT):\n    global SHARED\n    SHARED=x\n    return x\n',['assert m.f(False) is False','assert m.SHARED is False'],None,[]),
      ('explicit_old_bound',regular,['assert m.f(True) is True'],None,[0]),
      ('mixed_bound',regular,['assert m.f(False) is False\nassert m.f() is True'],None,[0]),
      ('defaults_metadata',regular,['assert m.f.__defaults__[0] is True'],None,[0]),
      ('kwdefaults_metadata','def f(*,x=DEFAULT):\n    return x\n',['assert m.f.__kwdefaults__["x"] is True'],None,[0]),
      ('signature',regular,['assert inspect.signature(m.f).parameters["x"].default is True'],None,[0]),
      ('alias_metadata',regular+'alias=f\n',['assert m.alias.__defaults__[0] is True'],None,[0]),
      ('dynamic_getattr',regular,['reader=getattr\nassert reader(m.f,"__"+"defaults__")[0] is True'],None,[0]),
      ('direct_getattribute',regular,["assert m.f.__getattribute__('__defaults__')[0] is True"],None,[0]),
      ('object_getattribute',regular,["assert object.__getattribute__(m.f,'__defaults__')[0] is True"],None,[0]),
      ('c_attrgetter',regular,["assert operator.attrgetter('__defaults__')(m.f)[0] is True"],None,[0]),
      ('kw_c_attrgetter','def f(*,x=DEFAULT):\n    return x\n',["assert operator.attrgetter('__kwdefaults__')(m.f)['x'] is True"],None,[0]),
      ('collection_metadata',regular+'CACHED=f.__defaults__[0]\n',['assert m.CACHED is True','assert True'],None,[0,1]),
      ('collection_opposite',regular+'CACHED=f(False)\n',['assert m.CACHED is False','assert True'],None,[]),
      ('fixture_opposite',regular,['assert m.CACHED is False','assert True'],'m.CACHED=m.f(False)',[]),
      ('collection_default',regular+'CACHED=f()\n',['assert m.CACHED is True','assert True'],None,[0,1]),
      ('fixture_metadata',regular,['assert m.CACHED is True','assert True'],'m.CACHED=inspect.signature(m.f).parameters["x"].default',[0,1]),
      ('shared_global','SHARED=None\ndef f(x=DEFAULT):\n    global SHARED\n    SHARED=x\n',['m.f()\nassert m.SHARED in (True,False)','assert m.SHARED is True'],None,[0,1]),
      ('caller_return_escape',regular+'SHARED=None\n',['m.SHARED=m.f()\nassert m.SHARED in (True,False)','assert m.SHARED is True'],None,[0,1]),
      ('latent_escape_branch',regular+'SHARED=None\n',['v=m.f()\nif not v:m.SHARED=False\nassert v in (True,False)','assert m.SHARED is None'],None,[0,1]),
      ('default_table_mutated',regular,['m.f.__defaults__=(False,)\nassert m.f() is False'],None,[0]),
      ('same_code_new_function',regular,['g=types.FunctionType(m.f.__code__,m.f.__globals__,argdefs=(False,))\nassert g() is False'],None,[0]),
      ('replaced_function_code',regular+'def g(x):\n    return x\n',['m.f.__code__=m.g.__code__\nassert m.f() is True','assert True'],None,[0,1]),
      ('fresh_contains_shared_alias',regular+'STATE=[]\n',['box=[m.STATE]\nbox[0].append(m.f())','assert m.STATE == [True]'],None,[0,1]),
      ('fresh_rebound_to_shared',regular+'STATE=[]\n',['box=[]\nbox=m.STATE\nbox.append(m.f())','assert m.STATE == [True]'],None,[0,1]),
      ('unknown_helper_escape',regular+'STATE=[]\ndef remember(v):\n    STATE.append(v)\n',['m.remember(m.f())','assert m.STATE == [True]'],None,[0,1]),
      ('descriptor_escape',regular+'STATE=[]\nclass Holder:\n    @property\n    def value(self):return None\n    @value.setter\n    def value(self,v):STATE.append(v)\n',['obj=m.Holder()\nobj.value=m.f()','assert m.STATE == [True]'],None,[0,1]),
      ('inherited_helper_escape',regular+'STATE=[]\nclass Parent:\n    def remember(self,v):STATE.append(v)\nclass Child(Parent):pass\n',['obj=m.Child()\nobj.remember(m.f())','assert m.STATE == [True]'],None,[0,1]),
    ]
    results=[]
    with tempfile.TemporaryDirectory(prefix='defaults-v10-') as tmp:
      for index,(name,template,actions,fixture,expected) in enumerate(cases):
        folder=Path(tmp)/str(index);folder.mkdir();path=folder/'module.py'
        original=template.replace('DEFAULT','True');mutant=template.replace('DEFAULT','False');path.write_text(original,encoding='utf-8')
        observed,base_failed,observer=execute(original,path,actions,fixture,True);_,failed,_=execute(mutant,path,actions,fixture,False)
        assert not base_failed,name
        ids=[f'tests/toy.py::C::test_{i}' for i in range(len(actions))];allbits=(1<<len(ids))-1
        def bits(contexts):return sum(1<<i for i in range(len(ids)) if i in contexts or '@class:C' in contexts)
        lines={str(line):format(bits(ctx),'x') for line,ctx in observed.items() if bits(ctx)}
        maps={'source_commit':'synthetic','ids':ids,'source_hashes':{'module.py':hashlib.sha256(original.encode()).hexdigest()},'lines':{'module.py':lines},'collection_lines':{'module.py':[n for n,c in observed.items() if '@collection' in c]},'reads':{},'modules':{'module.py':format(allbits,'x')},'symbols':{},'attributes':{}}
        (folder/'maps.json.gz').write_bytes(gzip.compress(json.dumps(maps).encode()))
        unsafe=set()
        for i,action in enumerate(actions):
            fn=ast.parse('def test(self):\n'+''.join('    '+line+'\n' for line in action.splitlines())).body[0]
            if potential_writer(fn,known_calls={'m.f','m.alias','inspect.signature','getattr','reader'}):unsafe.add(i)
        functions={}
        for site,definition in observer.known.items():
            record=observer.records[site];parameters={}
            for parameter,slot in definition['parameters'].items():
                entry=record['parameters'][parameter]
                parameters[parameter]={'kind':slot['kind'],'default':slot['default'],'seen':format(bits(entry['seen']),'x'),'possible':format(bits(entry['possible']),'x'),'opaque':format(bits(entry['opaque']),'x'),'collection':'@collection' in entry['seen'],'fixture':'@class:C' in entry['seen'],'collection_possible':'@collection' in entry['possible'],'collection_opaque':'@collection' in entry['opaque'],'fixture_possible':'@class:C' in entry['possible'],'fixture_opaque':'@class:C' in entry['opaque']}
            functions[site]={'parameters':parameters,'metadata_readers':format(bits(record['metadata']),'x'),'metadata_collection':'@collection' in record['metadata'],'metadata_fixture':'@class:C' in record['metadata'],'identity_opaque':record['opaque'],'state_effect':'persistent' if any(isinstance(n,(ast.Global,ast.Nonlocal)) for n in ast.walk(ast.parse(original))) else 'closed'}
        profile={'format_version':1,'complete':True,'failures':[],'source_commit':'synthetic','source_hashes':maps['source_hashes'],'state_source_hashes':maps['source_hashes'],'ids':ids,'modes':['warm','cold','eager-import-collection'],'metadata_scope':'source-tests-inspect-getattr','state_guard_complete':True,'functions':functions,'persistent_writer_tests':format(bits(unsafe),'x'),'persistent_opaque_tests':'0','scope':'Synthetic actual observation; modes equivalent for these tiny modules, not a full suite artifact.'}
        profile.update(identity_audit_complete=True,identity_audit_modes=['warm','cold','eager-import-collection'])
        (folder/'default-dependencies.json.gz').write_bytes(gzip.compress(json.dumps(profile).encode()))
        tests=[{'id':identity,'number':i,'seconds':.1} for i,identity in enumerate(ids)]
        patch=''.join(difflib.unified_diff(original.splitlines(True),mutant.splitlines(True),'a/module.py','b/module.py'))
        base=Selector(folder,folder/'maps.json.gz',tests);candidate=Candidate(folder,folder/'maps.json.gz',tests,precision_defaults=True)
        selected=[r['number'] for r in candidate.select(patch)];baseline=[r['number'] for r in base.select(patch)]
        missing=sorted(set(failed)-set(selected));okay=selected==expected and not missing
        results.append({'name':name,'passed':okay,'selected':selected,'expected':expected,'baseline':baseline,'actual_mutant_failed':failed,'newly_omitted_failures':missing,'reason':candidate.defaults_last,'potential_writer_numbers':sorted(unsafe)})
    result={'passed':all(r['passed'] for r in results),'checks':results,'python':sys.version,'platform':platform.platform(),'module_sha256':hashlib.sha256((ROOT/'default_dependencies.py').read_bytes()).hexdigest(),'scope':'Real tiny Python executions and production Selector. Profile facts auto-observed; no mutation labels or expected reader bits supplied to candidate. Full SQLGlot suite validation is separate.'}
    with output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2);stream.write('\n')
    print(json.dumps({'passed':result['passed'],'checks':len(results),'failures':[r for r in results if not r['passed']]}))
    return 0 if result['passed'] else 1
class DefaultProfileExecutionTests(unittest.TestCase):
    def test_observed_dependencies_preserve_actual_mutant_failures(self):
        with tempfile.TemporaryDirectory(prefix="default-execution-check-") as temporary:
            output = Path(temporary) / "results.json"
            run_cases(output)
            result = json.loads(output.read_text(encoding="utf-8"))
            for row in result["checks"]:
                with self.subTest(case=row["name"]):
                    self.assertTrue(row["passed"], row)


if __name__ == "__main__":
    unittest.main()
