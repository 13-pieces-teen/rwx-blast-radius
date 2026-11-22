"""Join pinned AST global loads to existing baseline coverage; no execution."""
import argparse,gzip,hashlib,json,sys,tarfile,faulthandler
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from import_state_readers import build_name_profile
def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path);p.add_argument('--archive',type=Path)
    p.add_argument('--map',type=Path,default=ROOT/'artifacts/baseline-map.json.gz');p.add_argument('--output',type=Path,default=ROOT/'artifacts/import-state-name-readers.json.gz');a=p.parse_args()
    if a.output.exists():raise FileExistsError(a.output)
    baseline=json.loads(gzip.decompress(a.map.read_bytes()));sources={}
    if a.archive:
        with tarfile.open(a.archive)as archive:
            for path in baseline['source_hashes']:sources[path]=archive.extractfile(path).read().decode('utf-8').replace('\r\n','\n')
    else:
        for path in baseline['source_hashes']:sources[path]=(a.source/path).read_text(encoding='utf-8')
    for path,text in sources.items():
        if hashlib.sha256(text.encode()).hexdigest()!=baseline['source_hashes'][path]:raise ValueError('Source mismatch '+path)
    faulthandler.dump_traceback_later(30,repeat=True)
    result=build_name_profile(sources,baseline)
    faulthandler.cancel_dump_traceback_later()
    result['generator_hashes']={'import_state_readers.py':hashlib.sha256((ROOT/'import_state_readers.py').read_bytes()).hexdigest(),'tools/build_import_state_readers.py':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    result['baseline_map_sha256']=hashlib.sha256(a.map.read_bytes()).hexdigest()
    a.output.write_bytes(gzip.compress(json.dumps(result,sort_keys=True,separators=(',',':')).encode(),mtime=0))
    print(json.dumps({'output':str(a.output),'sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'reader_keys':len(result['readers']),'attribute_names':len(result['attribute_names']),'test_declaration_edges':len(result['test_declaration_edges'])}))
if __name__=='__main__':main()
