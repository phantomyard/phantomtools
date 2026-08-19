# PR #25 PhantomOrg — phantomyard review cotejo (2026-08-19)

Estado del cotejo entre el feedback real de phantomyard y el zip de GPT
(`phantomorg-review-fixes.zip`), y lo implementado en este trabajo (rondas 1 y 2).

**Estado final: todo resuelto.** 525 tests + 96 subtests verdes; ruff +
ruff format + mypy + bandit limpios; push a `feat/phantomorg` (PR #25,
MERGEABLE).

## Ronda 2 — re-review de kaieriksen sobre el head `8917216` (resueltos)

1. **`human_npubs` = grant de confianza** (phantomchat_gen.py:70).
   **RESUELTO**: nuevo campo `principal_npubs` (única fuente de `allowed_npubs`,
   vacío por defecto = fail-closed). `human_npubs`, bridge y relays son
   endpoints de ENTREGA, nunca promovidos a principal. Test multi-persona:
   `test_human_npubs_are_delivery_not_principal` +
   `test_principal_npubs_are_trusted_but_humans_are_not`.
2. **npubs reales en fixtures renombrados** (org.yaml, README, tests).
   **RESUELTO**: las 7 claves reales (actores, bridge, humano) sustituidas por
   npubs sintéticos generados con checksum bech32 válido, propagados por
   fixtures y tests.
3. **Prune borra archivos shared/runtime enteros** (target.py:928).
   **RESUELTO**: prune revierte SOLO regiones owned — "plain" se elimina,
   "merge" conserva todo fuera de los markers ORG, "seed"/runtime queda
   byte-for-byte. El directorio de persona NUNCA se elimina. Regresión
   `test_prune_preserves_runtime_state_byte_for_byte`.
4. **Eliminar `--reset`** (target.py:843).
   **RESUELTO**: `_deploy_reset` y el flag `--reset` eliminados por completo.
   Un persona fresco es una operación lifecycle del runtime, no un modo del
   compilador.
5. **No escribir `memory/norms.md` directamente** (target.py:560).
   **RESUELTO**: el drawer pasa a "seed" (se siembra una vez un puntero, nunca
   se sobrescribe). La norma completa va a `kb/procedures/comunicacion-agentes.md`
   con frontmatter OKF. El contenido conciso se emite en runtime vía
   `phantombot memory capture --tag norm`.

## Ronda 1 — review inicial de phantomyard (resueltos)

### Bloqueantes (P1 / Blocking / Architecture)

1. **Deploy reemplaza el directorio vivo completo** (target.py).
   **RESUELTO**: deploy aditivo (escribe solo archivos owned in-place, atómico
   por archivo, nunca mueve el directorio).
2. **`allowed_npubs` = grant de confianza** (phantomchat_gen.py).
   **RESUELTO**: principal-only (ver ronda 2).
3. **SOUL.md sin modelo de seguridad** (soul.j2).
   **RESUELTO**: sección "Security and trust" + voice/communication/
   working-memory fuera de los bloques ORG.
4. **Access levels = instrucciones, no enforcement** (soul.j2:31).
   **RESUELTO**: etiquetado "Non-enforcing" en el bloque security.
5. **Backup collision-safety** (target.py:306).
   **RESUELTO**: nombre de backup con sufijo UUID (sin colisión de timestamp).
6. **Última org sobreescribe artefactos globales** (target.py:273) — deploy-all
   reescribe scopes.json/HUMANS.md. **Documentado** (el contrato multi-org de
   artefactos globales queda para el issue diferido de phantombot; el backup del
   primer org es el estado pre-sesión real, preservado en el rollback).
7. **Stale actors/artefactos obsoletos** (build.py:629).
   **RESUELTO**: `_reconcile_stale_output` al reusar el out dir.
8. **CI no cableado** (ci.yml:7).
   **RESUELTO**: jobs en el workflow raíz + ruff/format/bandit/mypy/tests verdes.
9. **Material real en fixtures** (org.yaml:4).
   **RESUELTO**: fixtures sintéticos (Verdant Aquaponics Co-op / Harbor Capital
   Advisors) + npubs sintéticos.
10. **phantomchat.json tratado como derived state** (robertclawson).
    **RESUELTO**: seed-once (nunca se sobrescribe; el allowlist es runtime state).

### P2

11. **Partial deploy-all exit 0** (cli.py:1323) — **RESUELTO**.
12. **MISSING_PHANTOMCHAT nunca emitido** (phantomchat.py:213) — **RESUELTO** (GPT).
13. **Instalar `po`** (install.sh:128) — **RESUELTO** (GPT).

### Design / Should-fix

14. **MEMORY.md seed demasiado fino** (memory.j2) — **RESUELTO**.
15. **memory/norms.md missing de _SEED_FILES** (build.py:107) — **RESUELTO**.
16. **Norma de comunicación en KB equivocado** (build.py:587) — **RESUELTO**
    (kb/procedures con frontmatter OKF).
17. **Frontmatter legacy-spelled** (build.py:127-180) — **RESUELTO** (GPT).

## Bloqueante residual documentado (NO nuestro)

El tier `relay_npubs` (remitente untrusted que pasa por el threat judge) es
pendiente de phantombot/phantomchat (#400). PhantomOrg ya hace su parte
(allowed_npubs = solo `principal_npubs`; identidad de relay separada en el
bridge). El deploy aditivo no reabre ese hueco.
