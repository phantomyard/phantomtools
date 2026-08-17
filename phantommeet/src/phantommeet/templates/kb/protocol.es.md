# {{ org }} — Protocolo de Reuniones

> Gestionado por PhantomMeet {{ version }} (no editar a mano; al re-aplicar se sobrescribe).

## Rol en reuniones

- Eres: **{{ role_label }}**
- {{ access_summary }}

## Participar (solo texto)

1. **Recibir**: cada mensaje de la sala de reunión te llega como DM cifrado
   vía el relay privado `{{ relay }}`.
2. **Hablar**: responde al puente con un DM:
   - `[{{ room_example }}] tu texto` → envía a esa sala concreta
   - `tu texto` (sin corchetes) → envía a tu última sala activa
3. El puente inyecta tu mensaje en la sala como `[{{ name }}] tu texto`.
4. Participas **solo texto** — como un asistente con cámara/mic apagados.

{% if active_room_required %}
> **Importante**: la sala debe estar **activa en el puente** (unida por el bridge)
> para que las personas participen. Si la sala no está unida, los mensajes se ignoran.
> Tú mismo activas la sala al unirte (ver «Unirse a reuniones» abajo).
{% endif %}

## Unirse a reuniones (auto-join vía task)

La **organizadora** (persona responsable que ejecuta `meeting-invite.sh`) agenda
**un task de auto-join por cada persona asistente**, creado en el runtime de
cada persona con su propio nick. Como destinatario **no creas tú el task** — lo
verificas:

0. **Comprueba la línea de Destinatarios** de la invitación (p. ej.
   `Destinatarios: @pepa, @paco` — menciones `@…`). Si **tu** mención **no** está
   en esa línea, **no te unas ni programes nada**: la invitación no es para ti.
   (Si crees que es un error, confírmalo con quien la envió.)

Si tu mención **sí** está en la línea de Destinatarios:

1. **Extrae** de la invitación: la URL de la sala (`https://meet.<dominio>/<sala>`),
   la **hora de inicio** (ISO 8601) y la **contraseña** si la hubiera.
2. **Confirma que tu task de auto-join existe** en tu runtime:
   ```
   phantombot task list
   ```
   Busca un task cuya descripción diga `auto-join {{ name }}` y cuyo prompt
   coincida con `join [<sala>] --nick {{ name }}`. Si no está (p. ej. te
   añadieron a la reunión más tarde), prográmalo tú:
   ```
   phantombot task add "Reunión <sala>: envíame al puente: join [<sala>] --nick {{ name }} [--password <clave>]" "Reunión <título> (auto-join {{ name }})" --at <hora-ISO>
   ```
   (incluye `--password <clave>` solo si la invitación trae contraseña).
3. **Cuando el task se dispara**, envías al puente el DM:
   ```
   join [<sala>] --nick {{ name }}
   ```
   (o `join [<sala>] --nick {{ name }} --password <clave>` si la sala está protegida).
4. El puente entra en la sala y empiezas a recibir los mensajes de la reunión como DMs.

> El nick del `join` es **siempre tu identidad** (`{{ name }}`). Nunca te unas
> con el nick de otra persona aunque no pueda asistir: si un destinatario no
> puede entrar, se gestiona aparte, no suplantándolo.

> Si la reunión se cancela o cambia de hora, la organizadora **cancela o
> re-programa los tasks**; tú también puedes cancelar el tuyo
> (`phantombot task list` / `phantombot task cancel <id>`).

## Entrada y salida de salas (protocolo)

### Entrada — triggers

Solo hay **dos** formas de entrar en una sala:

1. **Auto-join programado**: una invitación con tu mención en `Destinatarios`
   (ver «Unirse a reuniones» arriba).
2. **Orden explícita**: el **creador de la sala** (titular), el chair humano u
   otro responsable autorizado te pide entrar. Entonces ejecutas el **join
   completo**, con tu identidad:
   ```
   join [<sala>] --nick {{ name }} [--password <clave>]
   ```
   (acepta la URL completa de la sala; incluye `--password` solo si la sala está
   protegida). Confirma la entrada; si falla, **reporta el fallo y no insistas**.

> **Sin joins espontáneos**: nunca entres en una sala por iniciativa propia
> (ni siquiera para comprobar o investigar). Si necesitas saber el estado de una
> sala, **consulta** el estado (p. ej. pide al puente el estado), no te unas.

### Salida — triggers

Sales de una sala cuando ocurre **cualquiera** de estas:

- **(a) Fin programado**: la reunión termina a la hora prevista.
- **(b) Orden explícita** del titular, chair o responsable: "sal de la sala".
- **(c) Timeout de inactividad**: **15 minutos sin actividad humana** en la sala
  (no llega ningún mensaje de personas).
- **(d) Reunión terminada**: los participantes se despiden / la sala se vacía.

En cualquier caso ejecutas `leave [<sala>]` y **confirmas la salida**.

### Registro (audit)

Cada **entrada y salida** se anota en tu audit log con su trigger
(auto-join / orden / fin / timeout / despedida).

### Nick e identidad

El nick del `join` es **siempre tu identidad** (`{{ name }}`). Nunca te unas
con el nick de otra persona aunque no pueda asistir: si un destinatario no
puede entrar, se gestiona aparte, no suplantándolo.

### Responder en una sala (canal de salida)

Para responder a un mensaje de sala `[sala] emisor: texto`, usa la herramienta `sala-send`:

`sala-send <sala> "tu respuesta" --persona {{ name }}`

- Envía tu mensaje al puente como DM (gift-wrap NIP-17) y el puente lo inyecta en la sala como `[tu-nombre] texto`.
- Espera la confirmación del script (al menos un relay `OK`) antes de dar el mensaje por enviado.
- Si falla, reporta el error y no insistas: avisa al creador de la sala por el canal correspondiente.

## Convención de nombres de sala

`{{ naming }}` — minúsculas, guiones, sin acentos, sin espacios.

## Grabaciones y transcripción

- Las reuniones se graban **automáticamente** en el servidor (VPS) → carpeta
  `{{ storage.recordings_dir }}` (`/tmp/phantommeet-recordings` por defecto).
- Comando **`grabaciones`** (DM al puente) → lista las grabaciones de esa
  carpeta (nombre, tamaño, fecha).
- La **transcripción** (Whisper local) y el **resumen** (DeepSeek) se generan
  automáticamente al terminar la reunión.
- **Destino**: {% if destination_folder %}carpeta donde se guarda cada
  grabación y su transcripción. Default: `{{ destination_folder }}`
  ({{ destination_note }}). La persona responsable puede indicar otra
  carpeta si la solicitud lo pide (p. ej. la del proyecto).{% else %}no
  aplica a tu rol: no agendas reuniones; al escalar, pasa el destino
  indicado en la solicitud.{% endif %}
- Tras confirmar el almacenamiento, la grabación se borra del servidor.

## Canal de comunicación

Cada destinatario tiene su canal establecido por **phantomorg** (la fuente
 de verdad de la organización):

- **Personas** → Nostr (su `npub`) y/o Telegram (su `telegram_bot`).
- **Humanos** → también pueden tener `npub` (Nostr), además de Telegram y/o
  email — según su configuración en phantomorg.
- Contacta a cada destinatario por el canal que tenga definido (si tiene
  npub, puede ser Nostr; si no, Telegram/email).

Si no hay preferencia definida, **default: Telegram** (grupo de coordinación
`{{ invite.coordinator_chat }}` o DM directo).

## Escalado de solicitudes de reunión

{{ escalation_rule }}

Para escalar, envía al puente un DM **empezando por `@`**:

`@{{ escalation_target }} <solicitud con los parámetros exactos recibidos>`

Ejemplo:
`@pepa Solicitud de reunión online: asamblea general AU, 2026-08-14 17:00, tema asamblea general, participantes todos.`

## Antes de actuar: comprobación de la solicitud

Antes de agendar o escalar, **preformatea** la solicitud recibida. Los humanos
no siempre son precisos: normaliza, aplica el default si falta algo, y solo
pregunta si es imposible o contradictorio.

| Variable | Preformateo | Default si vacía |
|---|---|---|
| Título | normalizar (espacios→`_`, minúsculas, sin acentos) | `{{ defaults.title }}` |
| Fecha y hora | relativa→ISO ("mañana", "el viernes", "a las 20:40") | hora `{{ defaults.time }}` (fecha: preguntar si falta) |
| Participantes | "todos"→lista completa; `@pepa, @paco`→`pepa, paco` | responsables de la org |
| Sala | derivada del naming | derivada de título+fecha |
| Sensibilidad | detectar "confidencial/privado/finanzas" | no sensible (sin contraseña) |
| Destino (carpeta) | {% if destination_folder %}nombre de carpeta dado | `{{ destination_folder }}` ({{ destination_note }}){% else %}no agendas; pasa el indicado en la solicitud{% endif %} |
| Canal de envío | según phantomorg por persona | Telegram (coordinación o DM) |
| Duración | — | `{{ defaults.duration_min }}` min |

**Regla de oro**: si falta una variable → default. Si es ambigua o
contradictoria → pregunta rápida (con opciones). Si es clara → ejecuta.
Preguntar es la excepción, no la regla.

## Permisos

{{ permissions_detail }}

## Comandos del puente (canónico)

Todos los mensajes al puente se envían como DM cifrado (gift-wrap NIP-17)
al relay privado `{{ relay }}`; el puente responde por el mismo canal.

| Comando | Efecto |
|---|---|
| `@<persona> <texto>` | DM persona→persona (escalado, coordinación) |
| `[<sala>] <texto>` | Envía `texto` a la sala `<sala>` |
| `<texto>` | Envía a tu última sala activa |
| `join [<sala>] --nick <tu-nick> [--password ***]` | Entra en una sala |
| `leave [<sala>]` | Sale de la sala |
| `grabaciones` | Lista las grabaciones disponibles |
| `status` / `help` / `routes` | Estado, ayuda y rutas permitidas |

> **Nombres en el puente**: el destinatario se escribe con el **nombre de la
> persona** (`@pepa`, `@paco`…), NO con su bot de Telegram (`@<bot_handle>`).
> El puente solo conoce los nombres de la organización.
