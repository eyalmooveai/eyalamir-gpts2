"""Shared Vertex AI Gemini call helper - the one place that constructs a
google.genai Client and makes a structured-JSON-output request, used by
custom_metrics.py (Custom test translation/Q&A/examples), bq_sql_console.py
(malformed-SQL fix advice), and sign_registry.py (best-effort visual
classification of a detected sign's color/shape/dimensions). Authenticates
via Vertex AI + Application Default Credentials, the same mechanism this
app's BigQuery/GCS/Vision calls already use - not an API key (see
custom_metrics.py's module docstring for the full "why", including why
this was switched from an earlier Anthropic/API-key implementation).

Two entry points, both going through the same private `_generate_structured()`:
`call_structured()` (text-only) and `call_structured_with_image()` (text +
one image, for a caller that needs the model to actually look at a photo).
Factored this way rather than duplicated per-caller: every caller needs the
identical Client construction, response_schema/.parsed mechanics (including
google-genai's quirk of leaving .parsed silently None on a validation
failure instead of raising - see call_structured()'s docstring), and
credentials/rate-limit error mapping, and letting those drift into separate
copies would be exactly the kind of thing this app's own CLAUDE.md warns
against elsewhere.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

# Must be a region Vertex AI Gemini is actually available in - "us-central1"
# is, and it also matches this app's own Cloud Run REGION (deploy.sh); the
# two don't have to match, but keeping them the same avoids a second region
# to reason about. gemini-2.5-flash (not -pro) was a deliberate choice for
# the first caller (Custom test classification) - see custom_metrics.py's
# comment for the reasoning (a narrow classification/translation task that
# doesn't need Pro-tier reasoning, and flash supports fully disabling
# thinking, which Pro cannot) - the same reasoning applies to the
# malformed-SQL fix-advice caller, which is also a bounded "explain this
# error against this schema" task, not open-ended reasoning.
GEMINI_LOCATION = "us-central1"
GEMINI_MODEL = "gemini-2.5-flash"

_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


class CredentialsNotConfigured(RuntimeError):
    """Vertex AI Application Default Credentials aren't available here -
    distinguished from other RuntimeErrors so callers can lead an error
    message with this actionable reason instead of trailing it after a
    more confusing one (see custom_metrics.resolve_custom_criterion's
    docstring for the bug this fixed)."""


def _generate_structured(
    contents, response_schema: type[_SchemaT], project: str, location: str, max_output_tokens: int,
    system_instruction: str | None = None,
) -> _SchemaT:
    """The actual Client-construction/request/parsing mechanics shared by
    call_structured() (text-only) and call_structured_with_image()
    (text+image) - factored out so the two never duplicate the
    credentials/rate-limit error mapping or the response.parsed-can-be-
    silently-None handling (see call_structured's own docstring for why
    that check matters). `contents` is whatever shape the caller has
    already built (a bare string, or a list mixing a string with one or
    more `types.Part`) - this function doesn't care which."""
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types
    from google.auth.exceptions import DefaultCredentialsError, RefreshError

    config_kwargs = dict(
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    if system_instruction is not None:
        config_kwargs["system_instruction"] = system_instruction

    try:
        client = genai.Client(vertexai=True, project=project, location=location)
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=types.GenerateContentConfig(**config_kwargs),
        )
    except (DefaultCredentialsError, RefreshError) as e:
        raise CredentialsNotConfigured("Vertex AI credentials aren't configured here") from e
    except genai_errors.APIError as e:
        if e.code == 429:
            raise RuntimeError("Gemini API rate limit hit - try again in a moment.") from e
        raise RuntimeError(f"Gemini API error: {e}") from e

    parsed = response.parsed
    if parsed is None:
        # response_schema validation failed silently (see google-genai's
        # GenerateContentResponse._from_response) rather than raising -
        # this is the one place that actually checks.
        finish_reason = None
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason
        if finish_reason == types.FinishReason.MAX_TOKENS:
            raise RuntimeError("Gemini's response was cut off before finishing (hit its output-token limit) - try rephrasing more concisely.")
        raise RuntimeError("Gemini didn't return valid structured output for that - try rephrasing.")
    return parsed


def call_structured(
    system_instruction: str, user_text: str, response_schema: type[_SchemaT], project: str,
    location: str = GEMINI_LOCATION, max_output_tokens: int = 8192,
) -> _SchemaT:
    """Makes one Vertex AI Gemini call with structured JSON output
    conforming to `response_schema` (a pydantic model), thinking fully
    disabled (this is always a bounded classification/explanation task
    here, never open-ended reasoning that would benefit from it - see the
    module docstring). Raises CredentialsNotConfigured when Vertex AI ADC
    isn't set up, or RuntimeError on any other API failure (rate limit,
    network, malformed output, hit the output-token limit, ...) - both
    messages are safe to show the user."""
    return _generate_structured(user_text, response_schema, project, location, max_output_tokens, system_instruction=system_instruction)


def call_structured_with_image(
    system_instruction: str, user_text: str, image_path: Path, response_schema: type[_SchemaT], project: str,
    location: str = GEMINI_LOCATION, max_output_tokens: int = 8192,
) -> _SchemaT:
    """Same as call_structured(), but the model also sees the image at
    `image_path` - for a task that needs it to actually look at a photo
    (sign_registry.py's best-effort color/shape/dimensions/height
    classification of a detected sign, the only caller needing multimodal
    input as of this writing), not just read text. `contents` as a list
    mixing a `types.Part` (the image) with a plain string is the
    documented google-genai shape for this (confirmed against the
    installed SDK's own `ContentListUnionDict` type, not assumed from
    memory - see GEMINI_MODEL's own comment on why this app verifies
    google-genai's shape live rather than trusting a training-time
    recollection of it). Raises the same CredentialsNotConfigured/
    RuntimeError as call_structured()."""
    from google.genai import types
    image_bytes = Path(image_path).read_bytes()
    mime_type = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
    contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), user_text]
    return _generate_structured(contents, response_schema, project, location, max_output_tokens, system_instruction=system_instruction)
