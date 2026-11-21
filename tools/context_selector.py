"""Experimental runtime consumer of frozen baseline per-context observations.

This is deliberately separate from production. No mutation labels or IDs enter
the selection algorithm. Source/reflection/structural dependencies are retained.
Cross-test state closure is not generally proved; experiments bound the evidence.
"""
import ast
import gzip
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from selector import Selector,parse_patch,apply_change
from tools.comparison_proof import comparison_edits
from tools.comparison_values import OPERATIONS,decode,encode

class ExperimentalSelector(Selector):
    def __init__(self,source,maps,tests,profile):
        super().__init__(source,maps,tests)
        self.context_profile=json.loads(gzip.decompress(Path(profile).read_bytes()))
        assert self.context_profile['source_commit']==self.map['source_commit']
        assert self.context_profile['map_ids']==self.map['ids']

    @staticmethod
    def equivalent(records,edit):
        if not records:return False
        for record in records:
            if record['opaque']or not record['observations']or record['operation']!=edit[2]:return False
            for left,right,expected in record['observations']:
                values=[decode(left),decode(right)]
                if type(expected)is not bool or OPERATIONS[edit[2]](*values)is not expected:return False
                for i,replacement in enumerate(edit[4]):
                    if replacement is not None:
                        old,new=replacement
                        if encode(values[i])!=encode(old):return False
                        values[i]=new
                if OPERATIONS[edit[3]](*values)is not expected:return False
        return True

    def select(self,patch):
        selected=super().select(patch)
        try:
            return self.refine(selected,patch)
        except (OSError,ValueError,SyntaxError,TypeError,KeyError,IndexError,RecursionError,OverflowError):
            # The experiment must not turn a valid conservative production
            # answer into a crash when a patch falls outside the proof domain.
            return selected

    def refine(self,selected,patch):
        changes=parse_patch(patch)
        if len(changes)!=1:return selected
        change=changes[0]
        if change.old!=change.new or change.old not in self.context_profile['source_hashes']:return selected
        if self.context_profile['source_hashes'][change.old]!=self.map['source_hashes'].get(change.old):return selected
        original,tree,_=self.load_source(change.old)
        modified,_,_=apply_change(original,change)
        new_tree=ast.parse(modified)
        edits=comparison_edits(tree,new_tree)
        if not edits:return selected
        records=[]
        for edit in edits:
            site=f'{change.old}:{edit[0]}:{edit[1]}'
            contexts=self.context_profile['sites'].get(site)
            if not contexts:return selected
            if '@collection'in contexts and not self.equivalent(contexts['@collection'],edit):return selected
            records.append((edit,contexts))
        protected=self.bits('reads',change.old)|self.reflection_impact(change.old,original,modified)
        protected|=self.structural_impact(change.old,tree,new_tree,original,modified)
        if change.old in self.collection_reads:protected=self.all_bits
        protected_numbers={number for position,number in self.numbers.items()if protected&(1<<position)}|self.unknown
        result=[]
        for test in selected:
            identity=test['id']
            if test['number']in protected_numbers or any(not self.equivalent(contexts.get(identity,[])+contexts.get('@class:'+identity.rsplit('::',1)[0],[]),edit)for edit,contexts in records):
                result.append(test)
        return result

class PrefixExperimentalSelector(ExperimentalSelector):
    """Restrict omissions to the baseline prefix before any possible divergence.

This relies on the full canonical execution order being the actual order. It
does not establish correctness for reordered/subset execution or incomplete
baseline observations. There is no case ID or expected-selection lookup.
"""
    def __init__(self,source,maps,tests,profile):
        super().__init__(source,maps,tests,profile)
        self.canonical=self.context_profile['canonical_items']
        self.positions={identity:i for i,identity in enumerate(self.canonical)}
        assert len(self.positions)==len(self.canonical)

    def refine(self,selected,patch):
        context_selected=super().refine(selected,patch)
        selected_ids={test['id']for test in selected}
        safe_ids=selected_ids-{test['id']for test in context_selected}
        if not safe_ids:return selected
        change=parse_patch(patch)[0]
        original,tree,_=self.load_source(change.old)
        modified,_,_=apply_change(original,change)
        new_tree=ast.parse(modified)
        edits=comparison_edits(tree,new_tree)
        blocked=[self.positions[test['id']]for test in context_selected]
        protected=self.bits('reads',change.old)|self.reflection_impact(change.old,original,modified)
        protected|=self.structural_impact(change.old,tree,new_tree,original,modified)
        for position,number in self.numbers.items():
            if protected&(1<<position):blocked.append(self.positions[self.map['ids'][position]])
        for test in self.tests:
            if test['number']in self.unknown:blocked.append(self.positions.get(test['id'],0))
        if change.old in self.collection_reads:return selected
        for edit in edits:
            contexts=self.context_profile['sites'][f'{change.old}:{edit[0]}:{edit[1]}']
            for context,records in contexts.items():
                if not context.startswith('@'):
                    if context not in selected_ids:return selected
                elif not self.equivalent(records,edit):
                    if context=='@collection':return selected
                    if context.startswith('@class:'):
                        members=[i for i,identity in enumerate(self.canonical)if identity.startswith(context[7:]+'::')]
                        blocked.append(min(members)if members else 0)
                    else:return selected
        cutoff=min(blocked,default=len(self.canonical))
        return [test for test in selected if test['id']not in safe_ids or self.positions[test['id']]>=cutoff]
