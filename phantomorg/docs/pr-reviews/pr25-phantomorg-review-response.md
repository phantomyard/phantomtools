# PR #25 PhantomOrg — phantomyard review cotejo (2026-08-19)

Estado del cotejo entre el feedback real de phantomyard y el zip de GPT
(`phantomorg-review-fixes.zip`), y lo implementado en este trabajo.

**Estado final: todo resuelto.** 522 tests + 116 subtests verdes; ruff +
ruff format + mypy + bandit limpios; push a `feat/phantomorg` (PR #25
actualizado, MERGEABLE).

## Hallazgos de phantomyard (consolidados de kaieriksen, lenaparkhodges, robertclawson)

### Bloqueantes (P1 / Blocking / Architecture)

1. **Deploy reemplaza el directorio vivo completo** (target.py) — el swap de
   directorio destruye `identity.json` (raíz HKDF del vault), `vault.sqlite`,
   `memory.sqlite`, `memory/`, `kb/`, daily files, etc. → viola CONTRIBUTING.md §1.
   **RESUELTO**: deploy aditivo (escribe solo archivos owned in-place, atómico
   por archivo, nunca mueve el directorio); `--reset` como operación destructiva
   explícita.
2. **`allowed_npubs` = grant de confianza** (phantomchat_gen.py) — incluía
   bridge + todos los actores + humanos → tier trusted, salta el judge.
   **RESUELTO** (GPT): solo human_npubs, greeted vacío.
3. **SOUL.md sin modelo de seguridad** (soul.j2) — faltaba two-tier trust,
   threat judge, prompt-injection, relación principal.
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
   Advisors) en org.yaml, docs, CHANGELOG y tests.
10. **phantomchat.json tratado como derived state** (robertclawson).
    **RESUELTO**: seed-once (nunca se sobrescribe; el allowlist es runtime state).

### P2

11. **Partial deploy-all exit 0** (cli.py:1323).
    **RESUELTO**: exit non-zero siempre que `failed` o `collided`.
12. **MISSING_PHANTOMCHAT nunca emitido** (phantomchat.py:213) — **RESUELTO** (GPT).
13. **Instalar `po`** (install.sh:128) — **RESUELTO** (GPT).

### Design / Should-fix

14. **MEMORY.md seed demasiado fino** (memory.j2).
    **RESUELTO**: seed enriquecido (Trust/boundaries, Recent, Durable facts,
    drawers).
15. **memory/norms.md missing de _SEED_FILES** (build.py:107) — **RESUELTO** (GPT).
16. **Norma de comunicación en KB equivocado** (build.py:587) — **RESUELTO** (GPT)
    (memory/norms.md marker + kb/procedures).
17. **Frontmatter legacy-spelled** (build.py:127-180) — **RESUELTO** (GPT)
    (index/concept + title/description/aliases).

## Qué implementó este trabajo (además de lo de GPT)

A. **Deploy aditivo** (Blocker 1 + 10): escribe SOLO archivos owned in-place
   (block-merge contra el target vivo), nunca mueve el directorio; `--reset`
   como operación destructiva explícita; phantomchat.json seed-once. Prune
   archiva archivos owned per-file y preserva la mente acumulada.
B. Backup collision-safe (timestamp + UUID).
C. Stale actor/artifact reconciliation en build.
D. CI en workflow raíz + ruff/format/mypy/bandit verdes.
E. Fixtures sintéticos.
F. Label non-enforcing en access levels.
G. Exit non-zero en partial deploy-all/build-all.
H. SOUL.md core identity + MEMORY.md seed enriquecido.

## Bloqueante residual documentado (NO nuestro)

El tier `relay_npubs` (remitente untrusted que pasa por el threat judge) es
pendiente de phantombot/phantomchat. PhantomOrg ya hace su parte (allowed_npubs
= solo humanos, identidad de relay separada). El deploy aditivo no reabre ese
hueco.
