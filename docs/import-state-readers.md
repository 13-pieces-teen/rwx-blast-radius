# Post-import field readers

`import_state_readers.StateReaders` maps the actual changed fields reported by
`tools.import_state.diff` to baseline test bits. `ImportStateReaders` is an alias;
both `.impact(changed, before, after)` and `.readers(...)` return
`(bitset, diagnostics)`. Diagnostic keys use the `reader_` prefix. Pass the full
diff rows to preserve `added`/`removed` semantics; bare field keys are also
accepted for value-change lookups.

Class fields use `path:ClassQualname:attribute`; module globals use `path::name`.
The snapshot contains only each class's own fields. The reader follows the
before/after MRO and expands a base-field change into derived classes whose
effective owner is still that base. Overrides stop that propagation. Existing
attribute observations use the actual owner's simple class name, so canonical
snapshot names are translated to that format. Same-object alias groups are
expanded transitively. Nested-name collisions in the historical profile can
produce extra readers rather than discard an observation.

For ordinary class fields, selection unions exact attribute readers, named
namespace-field readers, opaque namespace readers, and historical dictionary
readers absent from the newer namespace observation. Namespace key-only readers
are included for added/removed fields. An unobserved attribute uses baseline
coverage of same-name attribute accesses, intersected with consumers of the
specific receiver and its applicable derived classes. A read on an unrelated
class with the same attribute name cannot expand this fallback. It does not
automatically select every user of the class. Cached fields on a test class use
that class's canonical test identities because SQLGlot symbol observations do
not include test classes. Changes to `__bases__` or
`__mro__` additionally retain class users because hierarchy changes affect
operations such as `isinstance` without an explicit attribute load.

Collection-time SQLGlot propagation is represented by the post-import state
diff: a derived field is selected when its resulting value changes. Merely
appearing in `collection_attributes` or a class-body bare-name expression is
therefore not an automatic class-wide fallback. Test-module/class declarations
sit outside the SQLGlot import snapshot; static declaration edges propagate
imports and cached values to their downstream test readers. Supported function
default/decorator references also contribute declaration edges. This does not
model every possible effect of executing a test module's class headers.

`artifacts/import-state-name-readers.json.gz` comes from the untouched pinned
SQLGlot source and the existing baseline line-coverage map. The builder resolves
global names with Python's symbol tables, follows import/re-export aliases,
records module attribute/namespace accesses, and joins those source locations
to observed test bits. It reads no mutation patches, outcomes or expected
selections. Its 250 source/test hashes and 1621 identities must match the map.
The initial artifact contains 8516 reader keys, 3888 attribute names and 159
test-declaration dependency keys; SHA-256:
`800cca02fcc074e5b63f457f38adde3bdbcc5fd25673f7a5452848ac4607bd71`.
Rebuild with `tools/build_import_state_readers.py --source SOURCE --map MAP
--output OUTPUT` or the equivalent `--archive` input. Existing outputs are not
overwritten.

This is attribution for the fixed SQLGlot baseline. It is not complete arbitrary
Python dataflow: dynamically constructed attribute names without a matching
observed owner, arbitrary heap reflection, and import-time effects not expressed
by the snapshot or supported test declarations remain limitations. Existing
source-read, reflection, structural and runtime coverage protections are composed
by the caller. Native mutation validation remains separate from the small
reader regressions covering globals, shadowing, cached test values, inheritance,
overrides, aliases, namespace keys and comprehension reads.
