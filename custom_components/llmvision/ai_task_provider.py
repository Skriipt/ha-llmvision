"""Bridge prepared LLM Vision frames to Home Assistant's native AI Task API."""

import asyncio
import base64
import shutil
import tempfile
from pathlib import Path
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import AI_TASK_MODEL
from .providers import Provider


class HomeAssistantAITask(Provider):
    """Use an existing AI Task entity and its model/sampling configuration."""

    def __init__(self, hass: HomeAssistant, entity_id: str | None):
        super().__init__(hass, api_key="", model=AI_TASK_MODEL)
        self.entity_id = entity_id

    async def validate(self) -> None:
        """Validate locally, allowing the normal initial unknown state."""
        from homeassistant.components.ai_task import AITaskEntityFeature

        if not isinstance(self.entity_id, str) or not self.entity_id.startswith(
            "ai_task."
        ):
            raise ServiceValidationError("ai_task_invalid_entity")
        state = self.hass.states.get(self.entity_id)
        if state is None:
            raise ServiceValidationError("ai_task_missing_entity")
        if state.state == STATE_UNAVAILABLE:
            raise ServiceValidationError("ai_task_unavailable")
        required = (
            AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
        )
        features = state.attributes.get("supported_features", 0)
        if not isinstance(features, int) or features & required != required:
            raise ServiceValidationError("ai_task_missing_features")

    @staticmethod
    def _check_format(call: Any) -> None:
        # LLM Vision accepts JSON Schema, whereas HA uses selector/vol schemas.
        if call.response_format != "text" or call.structure:
            raise ServiceValidationError(
                "Home Assistant AI Task does not support LLM Vision JSON Schema output; "
                "use response_format: text without structure or another provider"
            )

    def _prepare_vision_data(self, call: Any) -> dict:
        self._check_format(call)
        memory = getattr(call, "memory", None)
        instructions = [memory.system_prompt if memory else self._get_system_prompt()]
        images = []
        if getattr(call, "use_memory", False) and memory:
            references = memory.memory_images
            labels = memory.memory_strings
            if len(references) != len(labels):
                raise ServiceValidationError(
                    "AI Task memory images/descriptions do not match"
                )
            if references:
                instructions.append(
                    "The following reference attachments and descriptions are context only. "
                    "Do not mention them in the response."
                )
            for image, label in zip(references, labels, strict=True):
                images.append(image)
                instructions.append(f"Attachment {len(images)} (reference): {label}")

        # Enumerate images, not zip: missing filenames must never drop frames.
        for index, image in enumerate(call.base64_images):
            images.append(image)
            label = (
                call.filenames[index]
                if index < len(call.filenames) and call.filenames[index]
                else f"Image {index + 1}"
            )
            instructions.append(f"Attachment {len(images)} (analysis frame): {label}")
        instructions.append(call.message)
        return {"instructions": "\n\n".join(instructions), "images": images}

    def _prepare_text_data(self, call: Any) -> dict:
        self._check_format(call)
        return {
            "instructions": self._get_title_prompt() + "\n\n" + call.message,
            "images": [],
        }

    async def title_request(self, call: Any) -> str:
        # Do not mutate max_tokens: AI Task does not expose that parameter.
        return await self._make_request(self._prepare_text_data(call))

    def _write_attachments(self, images: list[str]) -> tuple[Path, list[dict]]:
        """Write only prepared JPEGs to a private per-call local media directory."""
        public_dir = Path(self.hass.config.path("www")).resolve()
        directory = None
        for source_id, media_dir in self.hass.config.media_dirs.items():
            root = Path(media_dir).resolve()
            if root.is_relative_to(public_dir) or not root.is_dir():
                continue
            try:
                # mkdtemp creates mode 0700; names are unique across concurrent calls.
                directory = Path(tempfile.mkdtemp(prefix="llmvision-", dir=root))
            except OSError:
                continue
            break
        if directory is None:
            raise ServiceValidationError(
                "AI Task attachments require an existing writable private media_dirs "
                "directory outside Home Assistant's www directory"
            )
        try:
            attachments = []
            for index, image in enumerate(images):
                filename = f"{index + 1}.jpg"
                (directory / filename).write_bytes(
                    base64.b64decode(image, validate=True)
                )
                attachments.append(
                    {
                        "media_content_id": (
                            f"media-source://media_source/{source_id}/{directory.name}/{filename}"
                        ),
                        "media_content_type": "image/jpeg",
                    }
                )
            return directory, attachments
        except BaseException:
            shutil.rmtree(directory)
            raise

    async def _cleanup_attachments(self, write_job) -> None:
        """Wait for the writer even if its caller was cancelled, then remove files."""
        try:
            directory, _ = await write_job
        except (OSError, ValueError, ServiceValidationError):
            # The writer cleans partial files before raising to the caller.
            return
        await self.hass.async_add_executor_job(shutil.rmtree, directory)

    async def _make_request(self, data: dict) -> str:
        from homeassistant.components.ai_task import async_generate_data

        await self.validate()
        write_job = None
        try:
            async with asyncio.timeout(self.request_timeout):
                attachments = None
                if data["images"]:
                    write_job = self.hass.async_add_executor_job(
                        self._write_attachments, data["images"]
                    )
                    _, attachments = await asyncio.shield(write_job)
                result = await async_generate_data(
                    self.hass,
                    task_name="LLM Vision",
                    entity_id=self.entity_id,
                    instructions=data["instructions"],
                    attachments=attachments,
                )
                if not isinstance(result.data, str) or not result.data.strip():
                    raise ServiceValidationError(
                        "AI Task returned an invalid or empty text response"
                    )
                return result.data
        finally:
            if write_job is not None:
                cleanup = self.hass.async_create_task(
                    self._cleanup_attachments(write_job)
                )
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError
