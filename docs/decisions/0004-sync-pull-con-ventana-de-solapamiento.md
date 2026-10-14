# 0004. Sync: pull por `updated_at` con ventana de solapamiento

- Estado: aceptado (se implementa con el módulo `sync`)
- Fecha: 2026-10-14

## Contexto

`MODULES.md` define el pull como "todo lo que tenga `updated_at > last_pulled_at`", y el `timestamp` lo pone el servidor.

`updated_at` se asigna **dentro** de la transacción que escribe la fila, pero la fila solo es visible para otros al hacer **commit**. Si la transacción tarda, un pull puede pasar en medio:

1. T1 empieza a las 10:00:00.000 y escribe una fila con `updated_at = 10:00:00.000`.
2. Un pull a las 10:00:01 no ve la fila (T1 no ha hecho commit) y responde `timestamp = 10:00:01`.
3. T1 hace commit a las 10:00:02.
4. El siguiente pull pide `updated_at > 10:00:01`: **la fila de T1 no se envía nunca**.

### `clock_timestamp()` no lo arregla
`now()` es la hora de inicio de la transacción; `clock_timestamp()` es la hora en que se ejecuta la sentencia. Con `clock_timestamp()` la ventana se achica, pero no desaparece: entre el `UPDATE` y el `COMMIT` sigue habiendo tiempo (más sentencias, flush, red, el commit mismo). Cualquier valor asignado dentro de la transacción es anterior a su commit. Lo mismo pasa con una secuencia: el número se toma antes del commit.

## Decisión

1. **Ventana de solapamiento en el pull.** El servidor consulta `updated_at > last_pulled_at - SYNC_PULL_OVERLAP` y responde `timestamp` = hora del servidor al **inicio** del pull. El solapamiento (orden de minutos, valor exacto al implementar) debe ser mayor que la transacción de escritura más larga posible.
2. **Acotar la duración de las transacciones** para que el solapamiento sea una garantía y no una apuesta: `statement_timeout` e `idle_in_transaction_session_timeout` en la conexión, y nada de IO externo dentro de una transacción.
3. **El pull repite filas ya enviadas** (las del solapamiento). El cliente lo tolera con `sendCreatedAsUpdated: true` en `synchronize()` de WatermelonDB, y el servidor manda creadas y actualizadas juntas en `updated`, sin intentar distinguirlas (con solapamiento no se puede de forma confiable). Los borrados lógicos repetidos en `deleted` son idempotentes del lado del cliente.

`updated_at` se queda con `now()` (`TimestampMixin`): con el solapamiento, cambiar a `clock_timestamp()` no aporta.

## Alternativas descartadas
- `clock_timestamp()` o una secuencia: reducen la ventana, no la eliminan.
- Usar `xmin` / `pg_current_xact_id()` y `pg_snapshot_xmin()` para saber qué transacciones seguían abiertas: es exacto, pero complica el protocolo, depende de detalles internos de Postgres y no encaja con el `last_pulled_at` numérico de WatermelonDB.
- Serializar todas las escrituras de un usuario (lock por usuario): cuello de botella y no cubre escrituras de otros módulos.

## Consecuencias
- Cada pull reenvía hasta `SYNC_PULL_OVERLAP` de cambios: más tráfico, despreciable a esta escala.
- Requisito para el cliente: `sendCreatedAsUpdated: true` (anotado en `docs/CLIENT_NOTES.md`).
- Tests al implementar: una escritura que hace commit después de un pull concurrente debe aparecer en el pull siguiente.
