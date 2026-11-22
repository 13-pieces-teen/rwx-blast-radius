"""Merge successful pristine observations and conservative static guard facts."""
import argparse,gzip,hashlib,json
from collections import Counter,defaultdict
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,default=HERE/'profile-run-v3');parser.add_argument('--identity-run',type=Path,default=HERE/'profile-identity-v1');parser.add_argument('--guards',type=Path,default=HERE/'strict-guard-v2.json');parser.add_argument('--output',type=Path,default=ROOT/'artifacts/default-dependencies.json.gz');args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    base=json.loads(gzip.decompress((ROOT/'artifacts/baseline-map.json.gz').read_bytes()));guards=load(args.guards)
    assert guards['source_hashes']==base['source_hashes']
    modes=['warm','cold','eager-import-collection'];raw=[];identity=[];provenance={}
    for run,identity_only in ((args.run,False),(args.identity_run,True)):
      assert load(run/'inputs.json')==load(run/'inputs-after.json')
      for mode in modes:
        directory=run/mode;path=directory/'observations.json.gz';r=json.loads(gzip.decompress(path.read_bytes()));state=load(directory/'inspect-after.json')[0];host=load(directory/'host-result.json')
        assert r['complete'] is True and not r['failures'] and r['mode']==mode and r['ids']==base['ids'] and r['source_hashes']==base['source_hashes']
        assert r['lifecycle_complete'] and r['before_source_unchanged'] and state['State']['Status']=='exited' and state['State']['ExitCode']==0 and not state['State']['OOMKilled']
        assert host['cleanup_exit']==0 and not host['timed_out'] and host['wait_stdout'].strip()=='0'
        assert state['HostConfig']['CpusetCpus']=='2-3' and state['HostConfig']['Memory']==3*1024**3
        assert state['Image']=='sha256:88076b5c3c02633283124709e875d64f385436438a1ab63aac9dceac94f24a59'
        assert r.get('identity_only',False)==identity_only
        (identity if identity_only else raw).append(r);provenance[('identity-' if identity_only else '')+mode]={'observations_sha256':sha(path),'state_sha256':sha(directory/'inspect-after.json'),'host_sha256':sha(directory/'host-result.json'),'image':state['Image'],'python':r['python'],'pytest':r['pytest'],'reports':r['reports'],'generator_hashes':r['generator_hashes']}
    assert all(r['definitions']==raw[0]['definitions'] for r in raw+identity)
    positions={name:i for i,name in enumerate(base['ids'])};allbits=(1<<len(positions))-1;classbits=defaultdict(int)
    for name,i in positions.items():classbits[name.rsplit('::',1)[0]]|=1<<i
    def contexts(values):
        bits=0;collection=False;fixture=False
        for value in values:
            if value in positions:bits|=1<<positions[value]
            elif value.startswith('@class:') and value[7:] in classbits:bits|=classbits[value[7:]];fixture=True
            else:collection=True
        return bits,collection,fixture
    functions={};counts=Counter()
    for site,definition in raw[0]['definitions'].items():
        records=[r['records'][site] for r in raw+identity];metadata=set().union(*(set(r['metadata']) for r in records));bits,col,fix=contexts(metadata)
        item={'metadata_readers':format(bits,'x'),'metadata_collection':col,'metadata_fixture':fix,'identity_opaque':any(r['opaque'] for r in records),'state_effect':guards['sites'][site]['state'],'parameters':{}}
        for name,slot in definition['parameters'].items():
            entries=[r['records'][site]['parameters'][name] for r in raw];vals={key:set().union(*(set(e[key]) for e in entries)) for key in ('seen','possible','opaque')}
            seen,scol,sfix=contexts(vals['seen']);possible,pcol,pfix=contexts(vals['possible']);opaque,ocol,ofix=contexts(vals['opaque'])
            item['parameters'][name]={'default':slot['default'],'kind':slot['kind'],'seen':format(seen,'x'),'possible':format(possible,'x'),'opaque':format(opaque,'x'),'collection':scol,'fixture':sfix,'collection_possible':pcol,'collection_opaque':ocol,'fixture_possible':pfix,'fixture_opaque':ofix}
            if seen and not (possible|opaque|bits) and not (scol or sfix or col or fix) and not item['identity_opaque']:counts['all_observed_values_opposite_parameters']+=1
        functions[site]=item
    writers=sum(1<<positions[i] for i,r in guards['tests'].items() if r['state']=='persistent');opaque=sum(1<<positions[i] for i,r in guards['tests'].items() if r['state']=='opaque')
    result={'format_version':1,'complete':True,'failures':[],'source_commit':base['source_commit'],'source_hashes':{k:v for k,v in base['source_hashes'].items() if k.startswith('sqlglot/')},'state_source_hashes':base['source_hashes'],'ids':base['ids'],'modes':modes,'metadata_scope':'source-tests-inspect-getattr','state_guard_complete':True,'functions':functions,'persistent_writer_tests':format(writers,'x'),'persistent_opaque_tests':format(opaque,'x'),'state_guard_scope':guards['scope'],'provenance':provenance,'static_guard_sha256':sha(args.guards),'collector_inputs_sha256':sha(args.run/'inputs.json'),'execution_model':raw[0]['execution_model'],'summary':{'function_count':len(functions),'parameter_count':sum(len(r['parameters']) for r in functions.values()),'persistent_tests':writers.bit_count(),'opaque_tests':opaque.bit_count(),**counts}}
    result.update(identity_audit_complete=True,identity_audit_modes=modes)
    # Verify the consumer's strict schema before writing any release artifact.
    import sys;sys.path.insert(0,str(ROOT));from default_dependencies import valid_profile
    assert valid_profile(result,base)
    args.output.write_bytes(gzip.compress(json.dumps(result,sort_keys=True,separators=(',',':')).encode(),mtime=0))
    summary={'output':str(args.output),'sha256':sha(args.output),**result['summary'],'provenance':provenance}
    with (args.run/'merge-summary.json').open('x',encoding='utf-8')as f:json.dump(summary,f,indent=2)
    print(json.dumps(summary))
if __name__=='__main__':main()
