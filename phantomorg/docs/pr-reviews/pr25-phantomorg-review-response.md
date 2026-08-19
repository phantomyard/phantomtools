# PR #25 PhantomOrg — phantomyard review cotejo (2026-08-19)

Estado del cotejo entre el feedback real de phantomyard y el zip de GPT
(`phantomorg-review-fixes.zip`).

## Hallazgos de phantomyard (consolidados de kaieriksen, lenaparkhodges, robertclawson)

### Bloqueantes (P1 / Blocking / Architecture)

1. **Deploy reemplaza el directorio vivo completo** (target.py) — el swap de
   directorio destruye `identity.json` (raíz HKDF del vault), `vault.sqlite`,
   `memory.sqlite`, `memory/`, `kb/`, daily files, etc. → viola CONTRIBUTING.md §1.
   **GPT NO lo arregló** (lo declara en "Remaining").
2. **`allowed_npubs` = grant de confianza** (phantomchat_gen.py) — incluía
   bridge + todos los actores + humanos → tier trusted, salta el judge.
   **GPT SÍ lo arregló** (solo human_npubs, greeted vacío).
3. **SOUL.md sin modelo de seguridad** (soul.j2) — faltaba two-tier trust,
   threat judge, prompt-injection, relación principal.
   **GPT parcial** (añadió "Security and trust", pero faltan voice/communication/
   narration/memory-usage).
4. **Access levels = instrucciones, no enforcement** (soul.j2:31) — etiquetar
   como non-enforcing o implementar deny-by-default.
   **GPT NO lo arregló.**
5. **Backup collision-safety** (target.py:306) — timestamp ms + `copy2`
   sobreescribe; colisión → rollback restaura datos equivocados.
   **GPT NO lo arregló.**
6. **Última org sobreescribe artefactos globales** (target.py:273) — deploy-all
   reescribe scopes.json/HUMANS.md; solo sobrevive la última org.
   **GPT NO lo arregló.**
7. **Stale actors/artefactos obsoletos** (build.py:629) — reusar out dir deja el
   actor borrado; quitar npub/canales/humans deja phantomchat.json/norm/HUMANS.md
   viejos.
   **GPT NO lo arregló.**
8. **CI no cableado** (ci.yml:7) — workflow anidado nunca corre; ruff/mypy/bandit
   fallan localmente.
   **GPT NO lo arregló.**
9. **Material real en fixtures** (org.yaml:4) — nombres reales de org/personas.
   **GPT NO lo arregló.**
10. **phantomchat.json tratado como derived state** (robertclawson) — el
    allowlist es runtime state (TOFU/persistTrust); sobrescribirlo en cada deploy
    pierde confianza runtime. → **GPT NO lo arregló.**

### P2

11. **Partial deploy-all exit 0** (cli.py:1323) — `mutation_failed or ((failed or
    collided) and not ok)` → exit 0 si una org falla pero otras OK.
    **GPT NO lo arregló** (el bug sigue: `failed=1, ok=2` → exit 0).
12. **MISSING_PHANTOMCHAT nunca emitido** (phantomchat.py:213) — **GPT SÍ**.
13. **Instalar `po`** (install.sh:128) — **GPT SÍ**.

### Design / Should-fix

14. **MEMORY.md seed demasiado fino** (memory.j2) — ~5 líneas, sin trust rules /
    norms pointer / drawers. **GPT NO.**
15. **memory/norms.md missing de _SEED_FILES** (build.py:107) — **GPT SÍ**.
16. **Norma de comunicación en KB equivocado** (build.py:587) — **GPT SÍ**
    (memory/norms.md marker + kb/procedures).
17. **Frontmatter legacy-spelled** (build.py:127-180) — `type: home`,
    `atomic-note.md`, sin title/description/aliases. **GPT SÍ**.

## Qué arregló GPT (verificado: 520 tests + 49 subtests verdes)

- phantomchat_gen: allowed = solo human_npubs, greeted = [].
- soul.j2: sección "Security and trust" (parcial — falta identidad core).
- build.py: memory/norms.md en _SEED_FILES + marker-merge; frontmatter OKF
  (index/concept + title/description/aliases); norma → kb/procedures + memory/norms.md.
- phantomchat.py: MISSING_PHANTOMCHAT.
- install.sh: `po`.

## Qué queda por hacer (este trabajo)

A. **Deploy aditivo** (Blocker 1 + 10): reescribir deploy para escribir SOLO
   archivos owned in-place (block-merge contra el target vivo), nunca mover el
   directorio; `--reset` como operación destructiva explícita; phantomchat.json
   seed-once (runtime state). Prune archiva archivos owned, no la mente.
B. Backup collision-safe (timestamp→exclusive+UUID).
C. Multi-org global artifacts (scopes/HUMANS deterministas o namespace).
D. Stale actor/artifact reconciliation en build (reusar out dir).
E. CI en workflow raíz + ruff/mypy/bandit verdes.
F. Fixtures sintéticos (org.yaml + docs + tests).
G. Label non-enforcing en access levels (soul.j2).
H. Exit non-zero en partial deploy-all/build-all.
I. SOUL.md core identity (voice/comms/narration/memory-usage).
J. MEMORY.md seed enriquecido.
