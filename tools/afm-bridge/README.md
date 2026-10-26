# afm-bridge (`papyrus-afm`)

A small Swift CLI that exposes Apple's on-device Foundation Model (`FoundationModels`
framework, `SystemLanguageModel` + `LanguageModelSession`) to papyrus as JSON over stdin/stdout.
Output is **guided**: the request's JSON Schema is converted into a `DynamicGenerationSchema`, so
the model can only emit conforming JSON. The Python side is `src/papyrus/llm/apple.py`
(`AppleFMBackend`).

## Build

Requires macOS 26+ on Apple silicon with Apple Intelligence enabled. The Command Line Tools
are enough; full Xcode is not needed.

```sh
cd tools/afm-bridge
swift build -c release        # -> .build/release/papyrus-afm
```

Python finds the binary at `tools/afm-bridge/.build/release/papyrus-afm`, or at `$PAPYRUS_AFM_BIN`.

## Protocol

One-shot mode reads one JSON request on stdin and writes one JSON line. `--serve` reads
newline-delimited requests and answers each with one line, so the process stays warm across a
batch. The exit code is 0 even when the model fails; failures go in the JSON. Any `"id"` in a
request is echoed back.

```sh
echo '{"op":"availability","probe":true}' | .build/release/papyrus-afm
# {"available":true,"context_tokens":8192,"model":"coreAdvanced3","variant":"AFM 3 Core Advanced","ready":true,...}

echo '{"op":"count_tokens","text":"hello world"}' | .build/release/papyrus-afm
# {"ok":true,"tokens":2}

echo '{"op":"extract","instructions":"Extract metadata.","prompt":"Attention Is All You Need. Ashish Vaswani, Noam Shazeer. 2017.",
       "schema":{"type":"object","properties":{"title":{"type":"string"},"authors":{"type":"array","items":{"type":"string"}},
       "year":{"type":"integer"}},"required":["title","authors"]}}' | .build/release/papyrus-afm
# {"ok":true,"mode":"guided","result":{"title":"Attention Is All You Need",...},"usage":{...},"elapsed_ms":1527,...}
```

`extract` options (defaults in brackets):

| key | meaning |
|---|---|
| `instructions`, `prompt`, `schema` | Required, except `instructions`. |
| `temperature` [0] | 0 means greedy sampling. |
| `max_tokens` [1024] | Maximum number of response tokens. |
| `fit` [true] | Truncate the prompt to fit the context window, using the real tokenizer (macOS 26.4+). |
| `guardrails` ["permissive"] | `permissive` (`.permissiveContentTransformations`) or `default`. |
| `use_case` ["general"] | `general` or `contentTagging`. |
| `guardrail_fallback` [true] | If guided generation hits a guardrail, retry unguided (see below). |
| `busy_retries` [2] | Retries with backoff when the system refuses the request (see below). |
| `schema_mode` ["dynamic"] | `dynamic` is our converter. `codable` uses `JSONDecoder().decode(GenerationSchema.self)`, which is strict and needs `additionalProperties` on every object. |

Errors look like `{"ok":false,"error":"…","kind":"context_overflow|guardrail|unavailable|other","detail":…}`.

Supported schema subset:

* `type`: object, array, string, integer, number or boolean. A `["T","null"]` type is treated as T.
* Also `properties`, `required`, `items`, `enum` (as strings), `description`,
  `minimum`/`maximum`, `minItems`/`maxItems` and `pattern`.
* Property order follows `x-order`; the Python side adds it automatically.
* Properties missing from `required` become optional. The model often fills them anyway.

## Caveats (measured on macOS 27.0.1, M-series, 24 GB)

* **Context**: 8192 tokens (`SystemLanguageModel.contextSize`) for "AFM 3 Core Advanced". It
  was 4096 on macOS 26. Instructions, schema, prompt and output all count against it. The
  `context_overflow` error reports exact numbers.
* **System busy**: when the process is not a foreground app, `modelmanagerd` refuses
  requests under memory pressure. The error is `ModelManagerError 1013` with "CriticalMemoryPressure".
  While this lasts, `contextSize` reads 0 and the reported variant drops to `core3`.
  The bridge reports this as `kind=unavailable, detail=system_busy` after retrying.
* **Guardrails**: `.permissiveContentTransformations` only relaxes **String** generation.
  Guided generation still throws `guardrailViolation`, even on harmless text. The bridge
  therefore retries unguided, asking for JSON as text and parsing it, and the response is
  marked `"mode":"unguided_fallback"`.
* **No logprobs**: the API exposes no token probabilities.
