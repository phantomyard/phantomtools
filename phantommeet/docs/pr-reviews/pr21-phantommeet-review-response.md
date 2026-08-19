# PR #21 PhantomMeet — phantomyard re-review cotejo (2026-08-19)

Estado del cotejo tras el re-review del commit `7047493` (post-hardening).

## Estado previo: CHANGES_REQUESTED (kaieriksen 14:42, lenaparkhodges 14:20)

lenaparkhodges: la mayoría de hilos del hardening resueltos; bloquea por el
npub del bridge + los hilos de Kai.
kaieriksen: "8 hilos previos abiertos + 4 bloqueantes nuevos. Reproduje el
path escape, la ejecución de shell en render, y el partial apply. 11/11 tests
verdes pero el comportamiento sigue inseguro e internamente inconsistente."

## Estado tras este trabajo: RESUELTO (19 tests + lint/format/bandit verdes)

| # | Hallazgo | Archivo | Fix aplicado |
|---|---|---|---|
| 1 | bridge npub en `allowed_npubs` = grant de confianza (upstream #400) | apply.py:433 | `_patch_phantomchat` ya NO añade el npub a `allowed_npubs` (fail-closed); solo mueve el relay privado al frente. Documentado `relay_npubs` (#400) |
| 2 | docs/SPEC enseñan "confía en la línea de destinatarios" + task free-text | meeting-workflow.md:84, SPEC.md, example/base manifests | docs + manifests sanitizados: la invitación es informativa, el join lo impulsa la tarea programada propia |
| 3 | `_contained_path` solo en legacy; `install_tools` escapa | apply.py:532 | `_contained_dest` (traversal + symlink) en CADA dest de tool, ANTES de leer/renderizar |
| 4 | shell injection vía `invite.phantombot_bin` + heredoc terminable | meeting-invite.sh.j2:44 | filtro `shquote` (shlex.quote) en todos los escalares del manifest; card shell-quoted en vez de heredoc |
| 5 | password a stdout + `--password-file` filtra en dry-run | meeting-invite.sh.j2:243 | dry-run nunca lee el secreto real (vault Y file); el summary redacta la password |
| 6 | partial apply sin preflight | apply.py:682 | two-phase: preflight total (JSON, templates, dests, tools) → commit atómico (temp + os.replace) |
| 7 | `.orig.json` no reversible (snapshot congelado) | apply.py:783 | delta owned `.phantommeet-phantomchat.delta.json` + comando `pm unapply` |
| 8 | `COORD_GROUP` solo guard; `notify` emite a todos | meeting-invite.sh.j2:232 | claim corregido: "broadcast via phantombot notify" (sin target falso) |
| 9 | docs `--self-join`/`task add` ya no existen | meeting-workflow.md:66 | docs limpios (workflow mediado por operador) |
| 10 | `_upsert_kb` descarta prefijo entre frontmatter y marker | apply.py:711 | `_split_frontmatter` preserva prefijo + sufijo |
| 11 | banner "Superseded" antepuesto al frontmatter OKF | apply.py:651 | `_supersede_legacy_kb` inserta el banner DESPUÉS del frontmatter |
| 12 | `check_persona_state` no verifica versión/contenido/tools | infra.py:450 | compara el body generado de Meetings.md + presencia de tools + npub del bridge NO en allowed_npubs |

## Bloqueante upstream (NO nuestro)

El tier `relay_npubs` en phantombot (issue #400). Hasta que phantombot lo
implemente, el bridge no debe estar en `allowed_npubs` (fail-closed): las
reuniones vía DM del bridge no funcionan hasta entonces, pero el perímetro no
se debilita. Es el MISMO bloqueante que phantomorg #25.

## Tests de regresión añadidos

- `test_bridge_npub_never_in_allowed_npubs` / `test_patch_phantomchat_records_added_relay_delta`
- `test_contained_dest_refuses_path_escape` / `test_apply_refuses_tool_path_escape`
- `test_upsert_kb_preserves_prefix_and_suffix`
- `test_apply_preflight_aborts_before_partial_write`
- `test_unapply_reverses_phantomchat_relay`
- `test_meeting_invite_shell_quotes_manifest_scalars`
