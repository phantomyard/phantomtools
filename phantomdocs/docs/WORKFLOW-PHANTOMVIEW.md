# Workflow installation map

`pd workflow installation-map` exports declared dependencies in the version-1
installation manifest consumed by PhantomView. The interface was reviewed in
`salvaalba-dev/phantomyard-tools-private/phantomview/installation.py` and its
collector on 2026-10-12. No private implementation is copied into this package.

The export contains the selected protocol stages, artifact roles, declared tool
bindings and a separate organizational archiver before indexing processed
results. A reference-only protocol needs no archiver. This describes dependencies;
it does not observe running processes or claim readiness. New components use
`probe: {"type": "none"}` and show unknown runtime status in the collector.

It exports no jobs, request IDs, document references, route destinations, actor
assignments, filesystem paths or security classifications. Treat installation
configuration and its tool identifiers as administrative information. Job states
remain available through the category-authorized `pd workflow status --graph`
interface; publishing those observations into a shared view requires a separate
authorized receiver and is not enabled here.

## Operator handover

Use explicit candidate paths. `CURRENT_MANIFEST` is the existing manifest selected
by PhantomView's `installation_file` setting; inspect that setting rather than
guessing a deployment path. The command does not read PhantomView configuration,
change the setting, restart services or collect probes.

```bash
pd workflow installation-map \
  --installation "$CANDIDATE_INSTALLATION" \
  --base-manifest "$CURRENT_MANIFEST" > "$CANDIDATE_MANIFEST"
```

Use different input/output paths: shell redirection truncates its output before
the command reads inputs. Omit `--base-manifest` only when creating a new manifest
for an installation with no existing map. Custom registries use `--registry`.

Existing components, relations, tool/persona bindings and other base fields are
preserved. Matching generated entries are reused on subsequent exports; a
conflicting generated ID requires review instead of replacement. Generated IDs
are stable hashes of organization and protocol identities. The export rejects
reserved/duplicate component IDs, dangling or cross-organization relations and
the collector's limits of 150 components and 1,000 relations.

Review the candidate in the private PhantomView deployment workflow. Its
organization IDs must match the organizations collected there. Existing base
probes may still execute when the operator later runs PhantomView: preserving
them is not an approval of them. This public product exports a compatible map;
it does not modify the private UI or its deployment.
