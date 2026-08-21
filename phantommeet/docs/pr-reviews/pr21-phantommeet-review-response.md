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
| 1 | bridge npub en `allowed_npubs` = grant de confianza (upstream #400) | apply.py:512 | `_patch_phantomchat` registra el npub en `relay_npubs` (#423) y nunca toca `allowed_npubs`; mueve el relay privado al frente |
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
| 12 | `check_persona_state` no verifica versión/contenido/tools | infra.py:444 | compara el body generado de Meetings.md + presencia de tools + npub del bridge en `relay_npubs` (y NO en `allowed_npubs`) |

## Actualización (2026-08-21): `relay_npubs` aterrizó

El tier untrusted `relay_npubs` ya está en phantombot (#400 cerrado por #423).
`_patch_phantomchat` ahora registra el npub del bridge en `relay_npubs` (lista
paralela de menor confianza: el remitente pasa por el threat judge, se trata
como untrusted, nunca arma TOFU y responde como `shared` incluso en DM 1:1) y
sigue sin tocar `allowed_npubs`. `check-infra` valida la ruta real: npub en
`allowed_npubs` = FAIL, npub ausente de `relay_npubs` (para personas con acceso)
= FAIL. El delta owned (`.phantommeet-phantomchat.delta.json`) registra tanto el
relay como el npub añadidos, y `pm unapply` revierte ambos. Docs (README/SPEC)
alineados con la ruta `relay_npubs`.

## Notify password (2026-08-21)

El password de sala aún viajaba en el body del `phantombot notify` (broadcast
untargeted a todos los owners autorizados). Fix: `meeting-invite.sh` **declara**
la contraseña (`--password-vault`/`--password-file`) pero **nunca la lee ni la
emite**; `%PASSWORD_LINE%` renderiza el aviso "se comparte por separado" /
"shared separately". El secreto se entrega fuera del broadcast (canal dirigido).
Test nuevo: `test_meeting_invite_never_broadcasts_password` (phantombot falso
que captura argv; verifica que el body no contiene el secreto).

## Tests de regresión añadidos

- `test_bridge_npub_never_in_allowed_npubs` / `test_patch_phantomchat_records_added_relay_delta`
- `test_meeting_invite_never_broadcasts_password`
- `test_contained_dest_refuses_path_escape` / `test_apply_refuses_tool_path_escape`
- `test_upsert_kb_preserves_prefix_and_suffix`
- `test_apply_preflight_aborts_before_partial_write`
- `test_unapply_reverses_phantomchat_relay`
- `test_meeting_invite_shell_quotes_manifest_scalars`
