# PR #24 — PhantomBridge: respuesta a la revisión de phantomyard

Fecha: 2026-08-19

## Estado general

Se aplicaron los cambios de hardening solicitados y la suite de tests queda
**verde de extremo a extremo** (`npm test` → EXIT 0):

- `test-audit-hardening.js` — 10 passed
- `test-routing.js` — 14 passed
- `test-org-routing.js` — 18 passed
- `test-pause.js` — 9 ok
- `test-antiloop.js` — 54 ok (incluidos los 4 casos de envelope firmado)
- `test-giftwrap-adversarial.js` + `test-giftwrap-fix.js` — ok
- `test-pause-persist.js` — 11 ok (fuera del runner `npm test`, corre en verde)
- `test-backpressure-recovery.js` — 8 ok (fuera del runner, corre en verde)

## Puntos resueltos en esta revisión

1. **Envelope anti-loop forjable (robertclawson §3).** El `[env]` ahora se sella
   con un MAC HMAC-SHA256 sobre `(metadata + payload)` con la clave del bridge,
   verificado con `timingSafeEqual`. Un envelope sin `sig` válida se descarta y
   se inicia uno fresco (fail-closed). Los tests del envelope se actualizaron
   para firmar con la clave del bridge (`envelopeMac` exportado + helper
   `signEnvelope` en el test).

2. **org.yaml malformado fail-open (kaieriksen §3, robertclawson §3).**
   `loadOrgRouting()` ahora lanza (EINVALID) ante YAML roto, esquema sin
   `version: 1`, roles/actors/escalation_matrix inválidos, referencias
   rotas y actores malformados. `validateOrgReferences()` + `ORG_SCHEMA_VERSION`.

3. **Permissions documentadas pero no enforced (kaieriksen §2, robertclawson §2).**
   `permissions: null`/malformado es fail-closed (no cae a legacy); cada ruta
   controlada por agente (join/leave/inject/recordings) se limita por
   emisor + alcance de sala (AUDIT M01).

4. **Agentes obsoletos tras cambios en org.yaml (robertclawson §4).** Con
   `org.yaml` presente, es la única fuente de verdad de identidad/routing; los
   agents manuales de `config.json` se ignoran (MEDIO-5).

5. **Secretos long-lived sin permisos (kaieriksen §4, robertclawson §4).**
   `nsec`, `relayNsec`, `password` y el token admin se leen vía `readSecret()`
   desde fichero (`*File`) o inline, con `assertPrivateFile` (0600 o más
   estricto). Los temporales de config/state/pause se crean 0600.

6. **HTTP API sin autenticación (lenaparkhodges, robertclawson §4).**
   Todos los endpoints (incluidos `/status` y `/recordings`) exigen
   `Authorization: Bearer <admin-token>`. El helper MCP (`mcp-bridge.mjs`)
   valida el token y fija el bind a loopback.

7. **Downloads sin autenticar + symlinks (lenaparkhodges §recordings).**
   El listado de recordings usa `lstatSync` (no sigue symlinks) y filtra por
   nombre seguro; el secret de descarga se valida como fichero privado.

8. **Monkey-patch TLS global (lenaparkhodges §SHOULD).** Eliminado; se usa
   `xmpps://` con verificación de certificado real.

9. **Superficie "cualquier sala" (robertclawson §1, bridge.js:1182).**
   El bridge ignora mensajes de salas no gestionadas (`if (!rooms.has(room))`).

10. **Higiene del repo (robertclawson §6/§7).** Eliminados los scripts
    scratch de la raíz del paquete (21+). Sin `nsec` ni `npub` reales
    hardcodeados en lo que se versiona.

## Punto pendiente — NO corresponde a este PR (lado phantombot)

**`relay_npubs` / tier de remitente no-confiado en phantombot.**

El bloqueante de fondo que los tres revisores señalaron — los DMs del bridge
no deben llegar a la persona como un principal de confianza — queda resuelto
**en la mitad que corresponde al bridge**:

- El contenido de sala se publica con una **identidad de relay separada**
  (`nostr.relayNsecFile` / `relayNsec`), nunca con la clave del bridge principal.
  El bridge **rehúsa arrancar** en modo Jitsi sin esa identidad separada.
- El payload se estructura como `[phantombridge-relay:v1] {origin, room,
  speaker, text}` con `speaker`/`text` saneados (no interpolación cruda en una
  posición sintácticamente idéntica a un comando de agente).
- La sala no gestionada ya no expande el conjunto de salas.

Lo que **falta es del lado phantombot**, tal y como el propio revisor indicó:
*"that piece is ours, not yours, and I would rather build it than have you
work around its absence."* — es decir, un tier `relay_npubs` junto a
`allowed_npubs` en `phantomchat` para que la clave de relay del bridge sea
clasificada como *untrusted* y la persona la pase por el threat judge.

Hasta que ese tier exista en phantombot, el bridge **no debe apuntarse a
personas en producción**. No hay valor de configuración que haga seguro el
modelo actual sin ese cambio en el receptor.

## Acción solicitada

- phantomyard: implementar el tier `relay_npubs` en `phantomchat` (remitente
  permitido para *entregar* pero no tratado como principal; pasa por el
  threat judge). Es un cambio aditivo de ~una docena de líneas.
- Hasta entonces: mantener el bridge fuera de producción (o solo con salas de
  prueba y personas no sensibles).
