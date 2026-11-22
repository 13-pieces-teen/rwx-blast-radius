"""Create conservative guard facts from the pinned source, without mutations."""
import argparse,gzip,hashlib,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'identity_v1'))
from static_effects import analyze

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--map',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    baseline=json.loads(gzip.decompress(args.map.read_bytes()));texts={}
    for relative,expected in baseline['source_hashes'].items():
        text=(args.source/relative).read_text(encoding='utf-8')
        if hashlib.sha256(text.encode()).hexdigest()!=expected:raise ValueError('source identity mismatch '+relative)
        texts[relative]=text
    result=analyze(texts,baseline['ids']);result['source_hashes']=baseline['source_hashes']
    result['script_sha256']=hashlib.sha256((HERE/'static_effects.py').read_bytes()).hexdigest()
    result['support_sha256']=hashlib.sha256((HERE/'identity_v1/profile_support.py').read_bytes()).hexdigest()
    with args.output.open('x',encoding='utf-8')as stream:json.dump(result,stream,indent=2,sort_keys=True)
if __name__=='__main__':main()
