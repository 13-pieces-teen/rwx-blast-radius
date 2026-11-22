"""Baseline-only observers and conservative potential test-state write analysis."""
from __future__ import annotations
import ast
import builtins
from collections import defaultdict
import functools
import types

META={'__defaults__','__kwdefaults__'}
GETATTR=builtins.getattr
REGISTER='__rwx_default_register__'
READ='__rwx_default_read__'

def slots(node):
    result={};args=[*node.args.posonlyargs,*node.args.args]
    for index,value in enumerate(node.args.defaults):
        if isinstance(value,ast.Constant) and type(value.value) is bool:
            result[args[len(args)-len(node.args.defaults)+index].arg]={'kind':'positional','index':index,'default':value.value}
    for index,value in enumerate(node.args.kw_defaults):
        if isinstance(value,ast.Constant) and type(value.value) is bool:
            result[node.args.kwonlyargs[index].arg]={'kind':'keyword_only','index':index,'default':value.value}
    return result

def definitions(path,tree):
    result={}
    def visit(node,names=(),inside_function=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)):
                qual='.'.join((*names,child.name));parameters=slots(child)
                if type(child) is ast.FunctionDef and not inside_function and not child.decorator_list and parameters:
                    result[f'{path}:{child.lineno}:{qual}']={'path':path,'line':child.lineno,'name':qual,'parameters':parameters}
                visit(child,(*names,child.name),True)
            elif isinstance(child,ast.ClassDef):visit(child,(*names,child.name),inside_function)
            else:visit(child,names,inside_function)
    visit(tree)
    return result

class Instrument(ast.NodeTransformer):
    def __init__(self,path,known):self.path=path;self.known=known;self.scope=[]
    def visit_ClassDef(self,node):
        self.scope.append(node.name);self.generic_visit(node);self.scope.pop();return node
    def visit_FunctionDef(self,node):
        site=f'{self.path}:{node.lineno}:{".".join([*self.scope,node.name])}'
        self.scope.append(node.name);self.generic_visit(node);self.scope.pop()
        if site not in self.known:return node
        call=ast.Expr(value=ast.Call(func=ast.Name(id=REGISTER,ctx=ast.Load()),args=[ast.Name(id=node.name,ctx=ast.Load()),ast.Constant(site)],keywords=[]))
        return [node,ast.copy_location(call,node)]
    def visit_AsyncFunctionDef(self,node):
        self.scope.append(node.name);self.generic_visit(node);self.scope.pop();return node
    def visit_Attribute(self,node):
        self.generic_visit(node)
        if isinstance(node.ctx,ast.Load) and node.attr in META:
            return ast.copy_location(ast.Call(func=ast.Name(id=READ,ctx=ast.Load()),args=[node.value,ast.Constant(node.attr)],keywords=[]),node)
        return node

class DefaultObserver:
    def __init__(self,known,context):
        self.known=known;self.context=context;self.functions={};self.by_code={};self.by_object={};self.looked={}
        self.records={key:{'metadata':set(),'opaque':False,'parameters':{p:{'seen':set(),'possible':set(),'opaque':set(),'counts':defaultdict(int)} for p in value['parameters']}} for key,value in known.items()}
        self.by_location={(r['path'],r['line'],r['name']):site for site,r in known.items()}
        self.source_prefix=None
        self.internal=0
    def register(self,function,site):
        self.internal+=1
        try:self._register(function,site)
        finally:self.internal-=1
    def _register(self,function,site):
        if not isinstance(function,types.FunctionType):self.records[site]['opaque']=True;return
        code=function.__code__;previous=self.functions.get(site)
        if previous is not None and previous['function'] is not function:self.records[site]['opaque']=True
        if code in self.by_code and self.by_code[code]!=site:self.records[site]['opaque']=True
        self.by_code[code]=site;self.by_object[id(function)]=site
        self.functions[site]={'function':function,'code':code,'defaults':function.__defaults__,'kwdefaults':function.__kwdefaults__}
        for name,slot in self.known[site]['parameters'].items():
            try:value=function.__defaults__[slot['index']] if slot['kind']=='positional' else function.__kwdefaults__[name]
            except (TypeError,IndexError,KeyError):value=None
            if value is not slot['default']:self.records[site]['opaque']=True
    def target(self,value):
        if isinstance(value,types.MethodType):value=value.__func__
        if not isinstance(value,types.FunctionType):return None
        site=self.by_object.get(id(value))
        if site is None:
            site=self.by_code.get(value.__code__)
            if site is not None:self.records[site]['opaque']=True
        return site
    def read(self,value,field,*default):
        if isinstance(field,str) and field in META:
            site=self.target(value)
            if site is not None:self.records[site]['metadata'].add(self.context())
        return GETATTR(value,field,*default)
    def audit(self,event,args):
        if self.internal:return
        if event=='object.__getattr__' and len(args)>1 and args[1] in META:
            site=self.target(args[0])
            if site is not None:self.records[site]['metadata'].add(self.context())
        if event=='function.__new__' and args:
            site=self.by_code.get(args[0])
            if site is not None:self.records[site]['opaque']=True
    def observe_callable(self,value):
        seen=set()
        while id(value) not in seen:
            seen.add(id(value));site=self.target(value)
            if site is not None:self.records[site]['metadata'].add(self.context())
            if isinstance(value,functools.partial):value=value.func;continue
            wrapped=GETATTR(value,'__wrapped__',None)
            if wrapped is None:break
            value=wrapped
    def profile(self,frame,event,arg):
        if event!='call':return
        code=frame.f_code;site=self.by_code.get(code)
        if site is None:return
        self.internal+=1
        try:self._profile(frame,site,code)
        finally:self.internal-=1
    def _profile(self,frame,site,code):
        record=self.records[site];function=self.functions[site];fn=function['function'];context=self.context()
        identity=(fn.__code__ is code and fn.__defaults__ is function['defaults'] and fn.__kwdefaults__ is function['kwdefaults'])
        for name,slot in self.known[site]['parameters'].items():
            entry=record['parameters'][name];entry['seen'].add(context);entry['counts'][context]+=1
            try:current=fn.__defaults__[slot['index']] if slot['kind']=='positional' else fn.__kwdefaults__[name]
            except (TypeError,IndexError,KeyError):current=None
            value=frame.f_locals.get(name)
            if not identity or current is not slot['default'] or type(value) is not bool:
                entry['opaque'].add(context)
            elif value is slot['default']:entry['possible'].add(context)

def instrument_inspect(observer):
    """Observe public callable-metadata entrances before test modules import aliases."""
    import inspect
    originals={name:GETATTR(inspect,name) for name in ('signature','getfullargspec')}
    descriptor=inspect.Signature.__dict__['from_callable']
    def wrap(original):
        @functools.wraps(original)
        def call(obj,*args,**kwargs):observer.observe_callable(obj);return original(obj,*args,**kwargs)
        return call
    for name,original in originals.items():setattr(inspect,name,wrap(original))
    @classmethod
    def from_callable(cls,obj,*args,**kwargs):
        observer.observe_callable(obj);return descriptor.__func__(cls,obj,*args,**kwargs)
    inspect.Signature.from_callable=from_callable
    def restore():
        for name,original in originals.items():setattr(inspect,name,original)
        inspect.Signature.from_callable=descriptor
    return restore

MUTATORS={'append','extend','update','clear','pop','popitem','add','remove','discard','setdefault','insert','sort','reverse','__setitem__','__delitem__','__setattr__'}

def potential_writer(function, imported=(), known_calls=(), safe_self=False):
    """Mark possible persistent writes, including branches not executed at baseline.

    A fresh local is assigned exactly once, from a literal of immutable values.
    Nested container aliases and unknown calls are not assumed local.
    """
    params={n.arg for n in [*function.args.posonlyargs,*function.args.args,*function.args.kwonlyargs]}
    locals_={n.id for n in ast.walk(function) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}|params
    imported=set(imported)|{a.asname or a.name.split('.')[0] for n in ast.walk(function) if isinstance(n,(ast.Import,ast.ImportFrom)) for a in n.names}
    assignments=defaultdict(list)
    for node in ast.walk(function):
        if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Store):assignments[node.id].append(None)
    def literal(value):
        if isinstance(value,ast.Constant):return True
        if isinstance(value,(ast.Tuple,ast.List,ast.Set)):return all(literal(x) for x in value.elts)
        if isinstance(value,ast.Dict):return all(x is not None and literal(x) for x in [*value.keys,*value.values])
        return False
    fresh=set()
    for node in ast.walk(function):
        if isinstance(node,ast.Assign) and isinstance(node.value,(ast.List,ast.Dict,ast.Set)) and literal(node.value):
            fresh.update(t.id for t in node.targets if isinstance(t,ast.Name) and len(assignments[t.id])==1)
    def root(node):
        depth=0
        while isinstance(node,(ast.Attribute,ast.Subscript)):depth+=1;node=node.value
        return node.id if isinstance(node,ast.Name) else None,depth
    for node in ast.walk(function):
        if isinstance(node,(ast.Global,ast.Nonlocal)):return True
        if isinstance(node,(ast.Attribute,ast.Subscript)) and isinstance(node.ctx,(ast.Store,ast.Del)):
            name,depth=root(node)
            if name in fresh and depth==1:continue
            if safe_self and name=='self' and depth==1 and isinstance(node,ast.Attribute):continue
            return True
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Name) and node.func.id in {'setattr','delattr','exec','eval','globals','locals','vars'}:return True
            if isinstance(node.func,ast.Attribute) and node.func.attr in MUTATORS:
                name,depth=root(node.func.value)
                if name not in fresh or depth:return True
            name=ast.unparse(node.func)
            if name not in known_calls and not (isinstance(node.func,ast.Attribute) and node.func.attr in MUTATORS and isinstance(node.func.value,ast.Name) and node.func.value.id in fresh):return True
    return False

def persistent_test_contexts(test_sources,ids):
    """Static whole-body helper closure; no canonical-order dependency assumption."""
    unsafe=set();facts=[]
    for path,text in test_sources.items():
        tree=ast.parse(text);functions={};class_names={}
        imported={a.asname or a.name.split('.')[0] for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom)) for a in n.names}
        def collect(node,scope=()):
            for child in ast.iter_child_nodes(node):
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    key=(*scope,child.name);functions[key]=child
                    # Nested callbacks are inspected in their parent body.
                elif isinstance(child,ast.ClassDef):collect(child,(*scope,child.name))
                else:collect(child,scope)
        collect(tree)
        hazards={key for key,node in functions.items() if potential_writer(node,imported)}
        calls={}
        for key,node in functions.items():
            names={n.func.id for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
            methods={n.func.attr for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id in {'self','cls'}}
            calls[key]={other for other in functions if len(other)==1 and other[-1] in names or other[:-1]==key[:-1] and other[-1] in methods}
        changed=True
        while changed:
            old=set(hazards)
            hazards|={key for key,edges in calls.items() if edges&hazards};changed=hazards!=old
        for identity in ids:
            parts=identity.split('::')
            if parts[0]!=path:continue
            key=tuple(part.split('[')[0] for part in parts[1:])
            if key not in functions:unsafe.add(identity);facts.append({'id':identity,'reason':'unknown_test_syntax'});continue
            if key in hazards:unsafe.add(identity);facts.append({'id':identity,'reason':'potential_persistent_write'})
    return unsafe,facts
