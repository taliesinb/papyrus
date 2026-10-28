"""Model backends. Specs:

  extraction backends:  'lmstudio' | 'lmstudio:<model>' | 'apple' | 'auto'
  deciders:             see papyrus.llm.decide.get_decider ('jev:<url>', 'lmstudio:<model>', None=auto)

'auto' prefers LM Studio (starting its server if it is off, see
`lmstudio.ensure_server`) when it serves the model, then Apple's on-device
Foundation Models. The spec usually comes from `config.backend()`.
"""

from __future__ import annotations

from .base import Backend, BackendUnavailable


def get_backend(spec: str | None = "auto") -> Backend:
    spec = spec or "auto"
    name, _, arg = spec.partition(":")
    if name == "lmstudio":
        from .lmstudio import LMStudioBackend, ensure_server
        lm = LMStudioBackend(model=arg or None)
        ensure_server(lm.base_url)
        return lm
    if name == "apple":
        from .apple import AppleFMBackend
        return AppleFMBackend()
    if name == "auto":
        import sys

        from .lmstudio import LMStudioBackend, ensure_server
        lm = LMStudioBackend()
        if ensure_server(lm.base_url) and lm.available():
            return lm
        print(f"(LM Studio unavailable at {lm.base_url} or not serving {lm.model}; "
              "trying Apple Foundation Models)", file=sys.stderr)
        try:
            from .apple import AppleFMBackend
            a = AppleFMBackend()
            if a.available():
                return a
        except (ImportError, BackendUnavailable):
            pass
        raise BackendUnavailable("no LLM backend available (start LM Studio, or build tools/afm-bridge)")
    raise ValueError(f"unknown backend spec {spec!r}")
