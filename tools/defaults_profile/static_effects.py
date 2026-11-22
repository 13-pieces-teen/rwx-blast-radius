"""Conservative source-resolved call/write closure, without coverage or labels.

Unresolved calls are opaque, never a package-name whitelist. This deliberately
measures the currently defensible support before extending ownership semantics.
"""
import ast
from collections import defaultdict
from profile_support import potential_writer

def analyze(texts,ids):
    modules={p.removesuffix('.py').replace('/','.').removesuffix('.__init__'):p for p in texts}
    functions={};imports={};classes={};aliases={}
    for path,text in texts.items():
        module=path.removesuffix('.py').replace('/','.').removesuffix('.__init__');tree=ast.parse(text);names={}
        for n in tree.body:
            if isinstance(n,ast.Import):
                for a in n.names:names[a.asname or a.name.split('.')[0]]=a.name if a.asname else a.name.split('.')[0]
            elif isinstance(n,ast.ImportFrom):
                parts=module.split('.')[:-1] if not path.endswith('/__init__.py') else module.split('.')
                prefix='.'.join(parts[:len(parts)-n.level+1]+([n.module] if n.module else [])) if n.level else n.module or ''
                for a in n.names:
                    if a.name!='*':names[a.asname or a.name]=prefix+'.'+a.name
        imports[module]=names
        def visit(node,scope=()):
            for n in ast.iter_child_nodes(node):
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    key=module+'.'+'.'.join((*scope,n.name));functions[key]=(path,n,scope,module)
                elif isinstance(n,ast.ClassDef):classes[module+'.'+'.'.join((*scope,n.name))]=(module,n);visit(n,(*scope,n.name))
                else:visit(n,scope)
        visit(tree)
    def qualify(module,name):
        root,*rest=name.split('.');value=imports[module].get(root,module+'.'+root)
        return '.'.join([value,*rest])
    def resolve(module,name):
        seen=set();value=qualify(module,name)
        while value not in seen:
            seen.add(value)
            if value in functions:return value
            prefix,_,leaf=value.rpartition('.')
            if prefix in imports and leaf in imports[prefix]:value=imports[prefix][leaf]
            else:return None
        return None
    edges={};reasons={};sites={}
    for key,(path,node,scope,module) in functions.items():
        site=f'{path}:{node.lineno}:{".".join((*scope,node.name))}';sites[site]=key
        calls=set();unknown=[];known_spelling=set()
        for n in ast.walk(node):
            if not isinstance(n,ast.Call):continue
            name=ast.unparse(n.func);target=resolve(module,name)
            if isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id in ('self','cls') and scope:
                candidate=module+'.'+'.'.join((*scope,n.func.attr));target=candidate if candidate in functions else None
            if target:calls.add(target);known_spelling.add(name)
            else:unknown.append(name)
        # A precise body proof must reject *any* unresolved call; even an API
        # returning a value can write a hidden cache, descriptor or singleton.
        if potential_writer(node,known_calls=known_spelling):reasons[key]={'state':'opaque' if unknown else 'persistent','unknown_calls':sorted(set(unknown))}
        else:reasons[key]={'state':'closed','unknown_calls':[]}
        if node.decorator_list:reasons[key]={'state':'opaque','unknown_calls':['<decorated callable>']}
        # Python descriptors, iteration and overloaded operators are calls too.
        # Until receiver/operand ownership and identity are proven, syntactic
        # absence of ast.Call must not pretend these bodies have no effects.
        implicit=(ast.Attribute,ast.Subscript,ast.For,ast.AsyncFor,ast.With,ast.AsyncWith,ast.BinOp,ast.UnaryOp,ast.Compare,ast.BoolOp,ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp,ast.AugAssign,ast.Yield,ast.YieldFrom,ast.Await)
        if any(isinstance(n,implicit) for statement in node.body for n in ast.walk(statement)):
            reasons[key]={'state':'opaque','unknown_calls':sorted(set(unknown+['<unproven implicit Python call>']))}
        if any(isinstance(n,(ast.Global,ast.Nonlocal)) for n in ast.walk(node)):reasons[key]['state']='persistent'
        edges[key]=calls
    changed=True
    while changed:
        changed=False
        for key,calls in edges.items():
            if reasons[key]['state']=='closed':
                bad=[v for v in calls if reasons[v]['state']!='closed']
                if bad:reasons[key]['state']='opaque';reasons[key]['unclosed_callees']=sorted(bad);changed=True
    test_states={}
    for identity in ids:
        parts=identity.split('::');module=parts[0].removesuffix('.py').replace('/','.')
        key=module+'.'+'.'.join(p.split('[')[0] for p in parts[1:])
        test_states[identity]=reasons.get(key,{'state':'opaque','unknown_calls':['<unresolved test identity>']})
    return {'sites':{s:reasons[k] for s,k in sites.items()},'tests':test_states,'functions':len(functions),'resolved_edges':sum(map(len,edges.values())),
            'scope':'Static direct imports/module/same-class helpers; unresolved dynamic dispatch, external callables, decorators and unknown mutation aliases fall back. No namespace purity exemptions.'}
