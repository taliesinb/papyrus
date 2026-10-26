"""Model backends. Specs:

  extraction backends:  'lmstudio' | 'lmstudio:<model>' | 'apple' | 'auto'
  deciders:             see papyrus.llm.decide.get_decider ('jev:<url>', 'lmstudio:<model>', None=auto)

'auto' prefers LM Studio when it is running with the model available, then
Apple's on-device Foundation Models.
"""

from __future__ import annotations

from .base import Backend, BackendUnavailable


def get_backend(spec: str | None = "auto") -> Backend:
    spec = spec or "auto"
    name, _, arg = spec.partition(":")
    if name == "lmstudio":
        from .lmstudio import LMStudioBackend
        return LMStudioBackend(model=arg or None)
    if name == "apple":
        from .apple import AppleFMBackend
        return AppleFMBackend()
    if name == "auto":
        from .lmstudio import LMStudioBackend
        lm = LMStudioBackend()
        if lm.available():
            return lm
        try:
            from .apple import AppleFMBackend
            a = AppleFMBackend()
            if a.available():
                return a
        except (ImportError, BackendUnavailable):
            pass
        raise BackendUnavailable("no LLM backend available (start LM Studio, or build tools/afm-bridge)")
    raise ValueError(f"unknown backend spec {spec!r}")
