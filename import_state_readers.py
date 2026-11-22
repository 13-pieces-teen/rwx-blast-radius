"""Map changed post-import fields to baseline readers, not class users.

Collection-time SQLGlot propagation is represented by the actual state diff.
Static test declaration edges cover cached imports/fields outside that snapshot.
The optional name profile is built only from pinned source and baseline coverage.
"""
from __future__ import annotations
import ast
from collections import defaultdict, deque
import gzip
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import symtable


def build_name_profile(sources, baseline):
    """Attribute names and resolved global loads joined to executed line readers."""
    modules = {p.removesuffix('.py').replace('/', '.').removesuffix('.__init__'): p for p in sources}
    trees = {p: ast.parse(text, filename=p) for p, text in sources.items()}
    classes = {}
    for module, path in modules.items():
        def collect(body, scope=()):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    qual = '.'.join((*scope, node.name))
                    classes[module+'.'+qual] = path+':'+qual
                    collect(node.body, (*scope, node.name))
                elif isinstance(node, (ast.If, ast.Try)):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, ast.ClassDef): collect([child], scope)
        collect(trees[path].body)
    imports = {}
    for module, path in modules.items():
        aliases = {}
        def scan(nodes):
            for n in nodes:
                if isinstance(n, ast.Import):
                    for a in n.names: aliases[a.asname or a.name.split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
                elif isinstance(n, ast.ImportFrom):
                    parent = module.split('.') if path.endswith('/__init__.py') else module.split('.')[:-1]
                    prefix = '.'.join(parent[:len(parent)-n.level+1]+([n.module] if n.module else [])) if n.level else n.module or ''
                    for a in n.names:
                        if a.name != '*': aliases[a.asname or a.name] = prefix+'.'+a.name
                elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scan(list(ast.iter_child_nodes(n)))
        scan(trees[path].body); imports[module] = aliases
    @lru_cache(maxsize=None)
    def resolve(value):
        seen = set()
        original=value
        for _ in range(32):
            if value in seen or len(value)>4096:return original
            seen.add(value)
            if value in modules or value in classes: return value
            parts = value.split('.')
            replacement = None
            for i in range(len(parts)-1, 0, -1):
                prefix = '.'.join(parts[:i]); name = parts[i]
                if name in imports.get(prefix, {}):
                    replacement = '.'.join([imports[prefix][name], *parts[i+1:]]); break
            if replacement is None: return value
            value = replacement
        return original
    readers = defaultdict(int); attributes = defaultdict(int); opaque_modules = defaultdict(int)
    declaration_edges = defaultdict(set); dynamic_attributes = 0
    def symbol(value):
        value = resolve(value)
        if value in classes: return classes[value]
        prefix, _, name = value.rpartition('.')
        prefix = resolve(prefix)
        return modules[prefix]+'::'+name if prefix in modules else None
    for module, path in modules.items():
        table = symtable.symtable(sources[path], path, 'exec')
        lines = {int(k):int(v,16) for k,v in baseline.get('lines',{}).get(path,{}).items()}
        class Visitor(ast.NodeVisitor):
            def __init__(self): self.table=table; self.scope=(); self.runtime=False; self.references=set()
            def add(self,key,node):
                if key:
                    readers[key] |= lines.get(node.lineno,0); self.references.add(key)
            def qualified(self,node):
                if isinstance(node,ast.Name): return resolve(imports[module].get(node.id,module+'.'+node.id))
                if isinstance(node,ast.Attribute):
                    parent=self.qualified(node.value)
                    return resolve(parent+'.'+node.attr) if parent else None
                return None
            def visit_Name(self,node):
                if not isinstance(node.ctx,ast.Load):return
                try: entry=self.table.lookup(node.id)
                except KeyError:return
                if self.table.get_type()=='module' or entry.is_global():
                    self.add(path+'::'+node.id,node)
                    target=symbol(imports[module].get(node.id,module+'.'+node.id))
                    if target:self.add(target,node)
                elif self.table.get_type()=='class' and self.scope:
                    self.add(path+':'+'.'.join(self.scope)+':'+node.id,node)
            def visit_Attribute(self,node):
                self.generic_visit(node)
                if not isinstance(node.ctx,ast.Load):return
                attributes[node.attr] |= lines.get(node.lineno,0)
                parent=self.qualified(node.value)
                if parent in modules:
                    if node.attr=='__dict__':opaque_modules[modules[parent]]|=lines.get(node.lineno,0)
                    else:self.add(modules[parent]+'::'+node.attr,node)
                elif parent in classes:self.add(classes[parent]+':'+node.attr,node)
            def visit_Call(self,node):
                nonlocal dynamic_attributes
                self.generic_visit(node)
                if isinstance(node.func,ast.Name) and node.func.id in ('getattr','hasattr') and len(node.args)>=2:
                    target=self.qualified(node.args[0]); name=node.args[1]
                    if isinstance(name,ast.Constant) and type(name.value) is str:
                        attributes[name.value]|=lines.get(node.lineno,0)
                        if target in modules:self.add(modules[target]+'::'+name.value,node)
                        elif target in classes:self.add(classes[target]+':'+name.value,node)
                    else:dynamic_attributes|=lines.get(node.lineno,0)
                if isinstance(node.func,ast.Name) and node.func.id in ('vars','globals'):
                    target=self.qualified(node.args[0]) if node.args else module
                    if target in modules:opaque_modules[modules[target]]|=lines.get(node.lineno,0)
            def visit_ImportFrom(self,node):
                parent=module.split('.') if path.endswith('/__init__.py') else module.split('.')[:-1]
                prefix='.'.join(parent[:len(parent)-node.level+1]+([node.module] if node.module else [])) if node.level else node.module or ''
                for a in node.names:
                    key=symbol(prefix+'.'+a.name)
                    if key:
                        self.add(key,node)
                        if path.startswith('tests/') and not self.runtime:declaration_edges[key].add(path+'::'+(a.asname or a.name))
            def _definition(self,node):
                is_function=isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda))
                previous_references=self.references;self.references=set()
                if is_function:
                    for n in [*node.args.defaults,*[v for v in node.args.kw_defaults if v is not None],*getattr(node,'decorator_list',[])]:self.visit(n)
                else:
                    for n in [*node.bases,*node.keywords,*node.decorator_list]:self.visit(n)
                name=node.name if hasattr(node,'name') else 'lambda'
                header_references=self.references;self.references=previous_references|header_references
                if is_function and path.startswith('tests/') and not self.runtime:
                    target=path+(':'+'.'.join(self.scope)+':' if self.scope else '::')+name
                    for dependency in header_references:declaration_edges[dependency].add(target)
                    for line in range(node.body[0].lineno if isinstance(node.body,list) else node.body.lineno,node.end_lineno+1):readers[target]|=lines.get(line,0)
                child=next((t for t in self.table.get_children() if t.get_name()==name and t.get_lineno()==node.lineno),None)
                previous=(self.table,self.scope,self.runtime)
                if child:self.table=child
                self.scope=(*self.scope,name);self.runtime=self.runtime or is_function
                for n in node.body if isinstance(node.body,list) else [node.body]:self.visit(n)
                self.table,self.scope,self.runtime=previous
            visit_FunctionDef=_definition
            visit_AsyncFunctionDef=_definition
            visit_ClassDef=_definition
            visit_Lambda=_definition
            def _assign(self,node):
                old=self.references;self.references=set()
                if node.value:self.visit(node.value)
                dependencies=self.references;self.references=old|dependencies
                if path.startswith('tests/') and not self.runtime:
                    targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target,ast.Name):
                            key=path+(':'+'.'.join(self.scope)+':' if self.scope else '::')+target.id
                            for dep in dependencies:declaration_edges[dep].add(key)
                for target in node.targets if isinstance(node,ast.Assign) else [node.target]:self.visit(target)
            visit_Assign=_assign
            visit_AnnAssign=_assign
        Visitor().visit(trees[path])
    return {'format_version':1,'complete':True,'source_commit':baseline.get('source_commit'),'ids':baseline['ids'],
            'source_hashes':baseline['source_hashes'],'readers':{k:format(v,'x') for k,v in readers.items()},
            'attribute_names':{k:format(v,'x') for k,v in attributes.items()},
            'module_namespaces':{k:format(v,'x') for k,v in opaque_modules.items()},
            'dynamic_attributes':format(dynamic_attributes,'x'),
            'test_declaration_edges':{k:sorted(v) for k,v in declaration_edges.items()},
            'scope':'Static global/attribute/namespace loads joined to baseline line coverage; test import-time cached fields propagated; no mutation labels.'}


class StateReaders:
    def __init__(self,selector,name_profile=None):
        self.selector=selector;self.map=selector.map;self.last=None
        if name_profile is None:
            path=Path(__file__).resolve().parent/'artifacts/import-state-name-readers.json.gz'
            if path.exists():name_profile=json.loads(gzip.decompress(path.read_bytes()))
            else:
                source=selector.source
                texts={p:(source/p).read_text(encoding='utf-8') for p in self.map['source_hashes']}
                for p,t in texts.items():
                    if hashlib.sha256(t.encode()).hexdigest()!=self.map['source_hashes'][p]:raise ValueError('source mismatch '+p)
                name_profile=build_name_profile(texts,self.map)
        if not (name_profile.get('complete') and name_profile.get('ids')==self.map['ids'] and name_profile.get('source_hashes')==self.map['source_hashes'] and name_profile.get('source_commit')==self.map.get('source_commit')):
            raise ValueError('state reader profile mismatch')
        self.names=name_profile

    def _namespace(self,symbol,attribute,keys_changed):
        selector=self.selector
        bits=int(getattr(selector,'class_dict_readers',{}).get(symbol,'0'),16)
        profile=getattr(selector,'namespace_profile',None)
        if profile:
            bits &= ~int(profile['all']['readers'].get(symbol,'0'),16)
            bits |= int(profile['opaque']['readers'].get(symbol,'0'),16)
            bits |= int(profile['fields']['readers'].get(attribute,'0'),16)
            if keys_changed:bits |= int(profile.get('keys',{}).get('readers',{}).get(symbol,'0'),16)
        return bits

    @staticmethod
    def _record(key,record=None):
        value=dict(record or {});value.setdefault('key',key)
        if '::' in key:
            path,name=key.split('::',1);value.setdefault('kind','module');value.setdefault('path',path);value.setdefault('name',name)
        else:
            path,qual,name=key.rsplit(':',2);value.setdefault('kind','class');value.setdefault('path',path);value.setdefault('qualname',qual);value.setdefault('name',name)
        value.setdefault('name',value.get('attribute'));return value

    def impact(self,changed_fields,before=None,after=None):
        """Return (baseline-index bitset, diagnostics); never add class-use masks."""
        before=before or {};after=after or {};fields={**before.get('fields',{}),**after.get('fields',{})}
        if isinstance(changed_fields,dict):
            changed_fields=changed_fields.get('changed',changed_fields)
            if isinstance(changed_fields,dict):changed_fields=[self._record(k,v) for k,v in changed_fields.items()]
        queue=deque(self._record(v,fields.get(v)) if isinstance(v,str) else self._record(v['key'],v) for v in changed_fields)
        alias_edges=defaultdict(set)
        for snapshot in (before,after):
            aliases=snapshot.get('aliases',{})
            if isinstance(aliases,dict):
                for key,values in aliases.items():alias_edges[key].update(values)
            else:
                for group in aliases:
                    for key in group:alias_edges[key].update(k for k in group if k!=key)
        seen=set();bits=0;rows=[]
        while queue:
            row=queue.popleft();key=row['key']
            if key in seen:continue
            seen.add(key);value=int(self.names['readers'].get(key,'0'),16);routes=[]
            if row['kind']=='module':
                value |= int(self.names['module_namespaces'].get(row['path'],'0'),16)
                value |= int(self.names['attribute_names'].get(row['name'],'0'),16)
                routes.append('global-name-lines')
            else:
                qual=row.get('qualname',row.get('class',''));name=row.get('name',row.get('attribute'))
                symbol=row['path']+':'+row.get('class_name',qual.rsplit('.',1)[-1]);attribute=symbol+':'+name
                keys_changed=row.get('change') in ('added','removed')
                candidates={attribute};symbols={symbol}
                for snapshot in (before,after):
                    record=snapshot.get('fields',{}).get(key,{})
                    owner=record.get('defined_on') or row.get('defined_on')
                    if owner:
                        info=snapshot.get('classes',{}).get(owner,{})
                        path,ownerqual=owner.rsplit(':',1);owner_symbol=path+':'+info.get('class_name',ownerqual.rsplit('.',1)[-1])
                        candidates.add(owner_symbol+':'+name);symbols.add(owner_symbol)
                observed=False
                for candidate in candidates:
                    if candidate in self.map.get('attributes',{}):observed=True;value |= int(self.map['attributes'][candidate],16)
                    value |= self._namespace(candidate.rsplit(':',1)[0],candidate,keys_changed)
                if not observed:
                    # A line such as ``expression.arg_types`` observes many
                    # unrelated concrete classes. Its coverage is only a
                    # candidate reader set; intersect it with this receiver's
                    # observed consumers before adding it to the dependency.
                    receiver_bits=int(self.map.get('symbols',{}).get(symbol,'0'),16)
                    receiver=row['path']+':'+qual
                    # SQLGlot's symbol observer does not record unittest
                    # classes. Their identities already delimit the receiver
                    # for cached class fields propagated by declaration edges.
                    if row['path'].startswith('tests/'):
                        prefix=row['path']+'::'+qual.replace('.','::')+'::'
                        receiver_bits|=sum(1<<i for i,identity in enumerate(self.map['ids']) if identity.startswith(prefix))
                    owners={receiver,row.get('defined_on',receiver)}
                    for snapshot in (before,after):
                        classes=snapshot.get('classes',{})
                        for derived,info in classes.items():
                            mro=info.get('mro',[])
                            if receiver not in mro:continue
                            effective=next((c for c in mro if name in classes.get(c,{}).get('own_fields',[])),None)
                            if effective not in owners:continue
                            derived_path,derived_qual=derived.rsplit(':',1)
                            derived_symbol=derived_path+':'+info.get('class_name',derived_qual.rsplit('.',1)[-1])
                            receiver_bits|=int(self.map.get('symbols',{}).get(derived_symbol,'0'),16)
                    value |= int(self.names['attribute_names'].get(name,'0'),16) & receiver_bits
                    routes.append('same-name-attribute-lines-within-receiver')
                else:routes.append('observed-owner-attribute')
                if name in ('__mro__','__bases__'):
                    value |= int(self.map.get('symbols',{}).get(symbol,'0'),16)
                    routes.append('class-hierarchy-change')
                if not row.get('_inherited_expansion'):
                    owner_symbol=row['path']+':'+qual
                    for snapshot in (before,after):
                        classes=snapshot.get('classes',{})
                        for derived,info in classes.items():
                            if derived==owner_symbol:continue
                            mro=info.get('mro',[])
                            if owner_symbol not in mro:continue
                            effective=next((c for c in mro if name in classes.get(c,{}).get('own_fields',[])),None)
                            if effective!=owner_symbol:continue
                            derived_path,derived_qual=derived.rsplit(':',1)
                            queue.append({'key':derived+':'+name,'kind':'class','path':derived_path,'qualname':derived_qual,
                                          'class_name':info.get('class_name',derived_qual.rsplit('.',1)[-1]),'name':name,
                                          'defined_on':owner_symbol,'change':row.get('change','value'),'_inherited_expansion':True})
            bits|=value;rows.append({'key':key,'readers':format(value,'x'),'routes':routes})
            for derived in self.names.get('test_declaration_edges',{}).get(key,[]):queue.append(self._record(derived,fields.get(derived)))
            for alias in alias_edges.get(key,[]):queue.append(self._record(alias,fields.get(alias)))
        self.last={'reader_fields':rows,'reader_expanded_fields':len(seen),'reader_bits':format(bits,'x'),'reader_collection_propagation':'actual changed post-import fields; static test declaration edges'}
        return bits,self.last

    readers=impact


ImportStateReaders=StateReaders
