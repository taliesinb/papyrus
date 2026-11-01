"""Backend-neutral contracts for local language models.

Two kinds of question are asked of a document:

* **Extraction** (generative, structured): "what is the abstract", "who are the
  authors", "what topics does it cover". Answered by `Backend.extract`, which
  returns a dict validated against a (small) JSON Schema.

* **Decision** (Jev / "System One" style): a fixed set of options and we want a
  probability for each — "is this a paper, book, slides ...", "is this library
  material at all". Answered by `Classifier.classify`, which returns calibrated-ish
  probabilities per option rather than a single sampled answer.

Backends: LM Studio (OpenAI-compatible HTTP), Apple Foundation Models (Swift
bridge), and any Jev-compatible `/v1/systemone` endpoint.

A third contract, `Embedder` (LM Studio's `/v1/embeddings`, llm/embed.py), serves the screen
(screen.py): the classify stage's fast first step.

For the inference cache (`papyrus.inference`, which wraps any Backend or Classifier), an
implementation may also offer:

* a `seed: int = 0` keyword on `extract` / `classify` (passed only where the signature has it);
* `cache_params() -> dict`: settings that shape answers besides the arguments (its prompt
  layout's version, a classifier's method…), so changing one makes a new cache key;
* `model_build() -> dict | None`: what pins the weights (None: can't tell right now);
* `seed_honoured(build) -> bool | None`, `model_loaded() -> bool | None`;
* `last_usage` ({prompt_tokens, completion_tokens}) and `last_params` (what the last call did)
  after each call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


class BackendUnavailable(RuntimeError):
    """Raised when a backend is not reachable / not supported on this machine."""


class ModelBusy(BackendUnavailable):
    """The model refused the request for now and may accept it later: Apple Foundation Models
    under memory pressure (ModelManagerError 1013). A reason to back off, not a failure of the
    document."""


@dataclass(frozen=True)
class ModelInfo:
    backend: str  # "lmstudio" | "apple" | "jev"
    model: str  # model identifier as reported by the backend
    context_tokens: int | None = None

    @property
    def tag(self) -> str:
        """Stable provenance tag stored in the database, e.g. 'lmstudio:qwen3.5-9b-mlx'."""
        return f"{self.backend}:{self.model}"


@runtime_checkable
class Backend(Protocol):
    """A generative model that can fill a JSON object matching a schema."""

    def info(self) -> ModelInfo: ...

    def available(self) -> bool:
        """Cheap check (no generation) that the backend can be used right now."""
        ...

    def extract(
        self,
        *,
        instructions: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return a JSON object conforming to `schema`. (Implementations may take `seed`, see above.)

        `schema` uses a conservative JSON Schema subset that every backend must
        support: type object/array/string/integer/number/boolean, properties,
        required, items, enum, description. No $ref, oneOf, or formats.
        Raises BackendUnavailable if the backend cannot be reached.
        """
        ...


QuestionType = Literal["choice", "yesno", "score"]


@dataclass(frozen=True)
class Question:
    """One Jev-style typed question.

    * choice: `criteria` maps option key -> description; answer is a
      probability distribution over the keys.
    * yesno: `criteria` may be empty; answer has keys "yes" and "no".
    * score: answer has key "score" in [0, 1] (e.g. "how likely is this a
      research paper"); criteria optional rubric.
    """

    key: str
    type: QuestionType
    instructions: str
    criteria: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    key: str
    probabilities: dict[str, float]  # for choice/yesno; {"score": x} for score

    @property
    def best(self) -> str:
        return max(self.probabilities, key=self.probabilities.__getitem__)

    @property
    def confidence(self) -> float:
        return self.probabilities[self.best]


@runtime_checkable
class Classifier(Protocol):
    """Answers several typed questions about one `state` (text) in one go."""

    def info(self) -> ModelInfo: ...

    def available(self) -> bool: ...

    def classify(self, state: str, questions: list[Question]) -> dict[str, Answer]: ...


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into vectors (the screen, screen.py). `embed` returns one vector per text, in
    order; raises BackendUnavailable if the model can't be reached."""

    def info(self) -> ModelInfo: ...

    def available(self) -> bool: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
