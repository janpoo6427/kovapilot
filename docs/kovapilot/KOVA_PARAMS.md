# KOVA Params contract

`kova_params` is the versioned user-settings contract shared through the KovaLink gateway. The bundled catalog owns presentation metadata; the persistent `KovaParams` entry owns only current values and a monotonic revision.

## Catalog

The catalog is `openpilot/kovapilot/config/kova_params.json` and follows `kova_params.schema.json`.

- `groups` organize settings without encoding UI layout.
- `title` and `description` contain text for every declared locale.
- `type` and `defaultValue` define the value contract.
- `risk`, `applyPolicy`, and `availability` support safety-aware gateway behavior.
- Unknown catalog fields are allowed so later metadata can be added compatibly.

The first sample is `steering.always_on` (`항시 조향`). It defaults to `false`, can only be changed offroad, and reports `pendingConsumer`. Its desired value can be stored and returned through the gateway, but it does not enable steering behavior until a separately reviewed vehicle-capability consumer exists.

## Persistent state

The `KovaParams` Params key is JSON:

```json
{
  "schemaVersion": 1,
  "revision": 1,
  "values": {
    "steering.always_on": true
  }
}
```

Writes require the caller's `expectedRevision`, reject stale updates, validate the exact declared type, enforce the apply policy, and use a blocking Params write. Invalid stored state fails closed and is not silently replaced.

When a catalog revision adds a setting, an older state that lacks that key receives the new setting's default without an eager storage write. Unknown stored keys still fail closed so removed or misspelled control settings cannot be ignored silently. The gateway process must own one shared `KovaParamsStore` instance as the single writer; its lock and revision check serialize writes within that process.

The gateway read response should use `KovaParamsStore.document()`, which returns the full catalog plus `revision`, each current `value`, and `applyStatus`. The gateway write handler should call `update()` with the device's authoritative offroad state; it must not trust an offroad flag supplied by KovaLink.
