"""Offline adapter tests using real HA AI Task and local media resolution.

Only the final model entity is synthetic. No model/network call is made and no
AI Task API or attachment resolver is mocked in the native-path tests.
"""

import asyncio
import base64
import io
import logging
import os
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.ai_task import AITaskEntity, AITaskEntityFeature
from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.media_source import async_resolve_media
from homeassistant.components.media_source.const import MEDIA_SOURCE_DATA
from homeassistant.components.media_source.local_source import (
    LocalMediaView,
    LocalSource,
)
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import EntityComponent
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llmvision import ServiceCallData, async_setup_entry, setup
from custom_components.llmvision.ai_task_provider import HomeAssistantAITask
from custom_components.llmvision.config_flow import llmvisionConfigFlow
from custom_components.llmvision.const import (
    AI_TASK_MODEL,
    CONF_AI_TASK_ENTITY,
    CONF_PROVIDER,
    DOMAIN,
    PROVIDER_AI_TASK,
)
from custom_components.llmvision.memory import Memory
from custom_components.llmvision.providers import OpenAI, ProviderFactory, Request


class SyntheticAITask(AITaskEntity):
    """A deterministic model at the far side of HA's native attachment path."""

    _attr_supported_features = (
        AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )
    _attr_name = "Synthetic vision"
    _attr_unique_id = "synthetic-vision"

    def __init__(self):
        self.tasks = []
        self.contents = []
        self.result = "Five synthetic frames analysed"
        self.handler = None

    async def _async_generate_data(self, task, chat_log):
        self.tasks.append(task)
        self.contents.append(
            await self.hass.async_add_executor_job(
                lambda: [
                    attachment.path.read_bytes()
                    for attachment in task.attachments or []
                ]
            )
        )
        if self.handler:
            return await self.handler(task)
        return GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=self.result
        )


@pytest.fixture
def frames():
    """Five distinct, valid JPEGs generated entirely in memory."""
    encoded = []
    for index in range(5):
        data = io.BytesIO()
        Image.new("RGB", (8, 8), (index * 40, 20, 200)).save(data, format="JPEG")
        encoded.append(base64.b64encode(data.getvalue()).decode())
    return encoded


@pytest.fixture
async def native(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    hass.config.media_dirs = {"local": str(media)}
    hass.data[MEDIA_SOURCE_DATA] = {
        "media_source": LocalSource(
            hass, "media_source", "Media", hass.config.media_dirs, "/media"
        )
    }
    component = EntityComponent(logging.getLogger(__name__), "ai_task", hass)
    hass.data["ai_task"] = component
    entity = SyntheticAITask()
    await component.async_add_entities([entity])
    hass.data[DOMAIN] = {
        "settings": {
            CONF_PROVIDER: "Settings",
            "system_prompt": "Custom system instructions",
            "title_prompt": "Custom title instructions",
        },
    }
    MockConfigEntry(domain=DOMAIN, data=hass.data[DOMAIN]["settings"]).add_to_hass(hass)
    adapter_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="adapter",
        data={CONF_PROVIDER: PROVIDER_AI_TASK, CONF_AI_TASK_ENTITY: entity.entity_id},
    )
    adapter_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, adapter_entry)
    assert hass.data[DOMAIN]["adapter"][CONF_AI_TASK_ENTITY] == entity.entity_id
    yield SimpleNamespace(hass=hass, entity=entity, media=media, tmp_path=tmp_path)
    await component.async_remove_entity(entity.entity_id)


def make_call(hass, frames=(), **overrides):
    call = ServiceCallData(
        ServiceCall(
            hass,
            DOMAIN,
            "image_analyzer",
            {
                "provider": "adapter",
                "message": "Describe changes in order",
                **overrides,
            },
        )
    )
    call.base64_images = list(frames)
    call.filenames = [f"frame-{index}.jpg" for index in range(len(frames))]
    call.memory = Memory(hass)
    return call


@pytest.mark.parametrize("count", [1, 5])
async def test_native_attachments_order_and_cleanup(native, frames, count):
    provider = HomeAssistantAITask(native.hass, native.entity.entity_id)
    # unknown is the real initial state of the real HA base entity.
    assert native.hass.states.get(native.entity.entity_id).state == "unknown"
    await provider.validate()
    assert (
        await provider.vision_request(make_call(native.hass, frames[:count]))
        == native.entity.result
    )
    task = native.entity.tasks[0]
    assert native.entity.contents[0] == [base64.b64decode(f) for f in frames[:count]]
    assert len(task.attachments) == count
    assert [a.mime_type for a in task.attachments] == ["image/jpeg"] * count
    assert [a.path.name for a in task.attachments] == [
        f"{i + 1}.jpg" for i in range(count)
    ]
    assert all(
        a.media_content_id.startswith("media-source://media_source/local/")
        for a in task.attachments
    )
    assert task.structure is None and task.llm_api is None
    assert task.instructions.startswith("System prompt: Custom system instructions")
    assert task.instructions.endswith("Describe changes in order")
    assert all(not a.path.exists() for a in task.attachments)
    assert list(native.media.iterdir()) == []
    assert LocalMediaView.requires_auth is True


async def test_native_resolver_rejects_data_urls(native):
    # Prove why base64 data URLs cannot be substituted for HA media sources.
    with pytest.raises(HomeAssistantError):
        await async_resolve_media(native.hass, "data:image/jpeg;base64,aW1n", None)


async def test_memory_mapping_and_missing_frame_names(native, frames):
    call = make_call(native.hass, frames, use_memory=True)
    call.filenames = ["first frame"]
    call.memory.memory_images = frames[:2]
    call.memory.memory_strings = ["Reference red", "Reference green"]
    await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(call)
    task = native.entity.tasks[0]
    assert native.entity.contents[0] == [
        base64.b64decode(f) for f in frames[:2] + frames
    ]
    assert "Attachment 1 (reference): Reference red" in task.instructions
    assert "Attachment 2 (reference): Reference green" in task.instructions
    assert "Attachment 3 (analysis frame): first frame" in task.instructions
    assert "Attachment 7 (analysis frame): Image 5" in task.instructions


async def test_memory_disabled_and_data_analyzer_prompt(native, frames):
    call = make_call(native.hass, frames[:1])
    call.memory = Memory(native.hass, system_prompt="Extract only the requested number")
    call.memory.memory_images = frames[1:]
    await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(call)
    assert len(native.entity.tasks[0].attachments) == 1
    assert native.entity.tasks[0].instructions.startswith(
        "System prompt: Extract only the requested number"
    )


async def test_mismatched_memory_is_rejected_without_dropping_images(native, frames):
    call = make_call(native.hass, frames, use_memory=True)
    call.memory.memory_images = frames[:2]
    call.memory.memory_strings = ["Only one description"]
    with pytest.raises(
        ServiceValidationError, match="images/descriptions do not match"
    ):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            call
        )
    assert native.entity.tasks == []
    assert list(native.media.iterdir()) == []


async def test_private_permissions_and_unwritable_directory_fallback(native, frames):
    blocked = native.tmp_path / "readonly"
    blocked.mkdir(mode=0o500)
    if os.access(blocked, os.W_OK):
        pytest.skip(
            "Requires an unprivileged test process to exercise permission denial"
        )
    native.hass.config.media_dirs = {
        "blocked": str(blocked),
        "local": str(native.media),
    }

    async def check_permissions(task):
        permissions = await native.hass.async_add_executor_job(
            lambda: [
                (a.path.parent.parent, stat.S_IMODE(a.path.parent.stat().st_mode))
                for a in task.attachments
            ]
        )
        assert permissions == [(native.media, 0o700)] * 5
        return GenDataTaskResult(conversation_id="synthetic", data="private")

    native.entity.handler = check_permissions
    call = make_call(native.hass, frames)
    call.memory = None
    assert (
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            call
        )
        == "private"
    )
    assert native.entity.tasks[0].instructions.startswith("Custom system instructions")
    assert list(native.media.iterdir()) == []
    assert list(blocked.iterdir()) == []


async def test_text_only_title_needs_no_media_directory(native, frames):
    native.hass.config.media_dirs = {}
    call = make_call(native.hass, frames)
    call.message = "Title for this summary"
    before = call.max_tokens
    await HomeAssistantAITask(native.hass, native.entity.entity_id).title_request(call)
    assert native.entity.tasks[0].attachments is None
    assert (
        native.entity.tasks[0].instructions
        == "Custom title instructions\n\nTitle for this summary"
    )
    assert call.max_tokens == before


@pytest.mark.parametrize("model", [None, "glimpse-v1", "gemini-2.5-flash"])
async def test_request_routing_ignores_blueprint_models_and_keeps_titles(
    native, frames, model
):
    call = make_call(native.hass, model=model, generate_title=True)
    req = Request(native.hass, call.message, call.max_tokens, call.temperature)
    for frame in frames:
        req.add_frame(frame, "")
    # This response would be consumed by Glimpse-specific JSON postprocessing.
    native.entity.result = '{"title":"Camera title","description":"Camera description"}'
    result = await req.call(call)
    assert call.model == AI_TASK_MODEL and not call.model_is_glimpse()
    assert result["response_text"] == native.entity.result
    assert "title" in result
    assert len(native.entity.tasks) == 2
    assert native.entity.tasks[1].attachments is None
    assert isinstance(
        ProviderFactory.create(
            native.hass,
            PROVIDER_AI_TASK,
            native.hass.data[DOMAIN]["adapter"],
            "glimpse-v1",
        ),
        HomeAssistantAITask,
    )


@pytest.mark.parametrize(
    "response_format,structure",
    [
        ("json", None),
        ("json", {"type": "object"}),
        ("json", "{"),
        ("text", {"type": "object"}),
    ],
)
async def test_json_schema_is_explicitly_unsupported(
    native, frames, response_format, structure
):
    call = make_call(
        native.hass, frames, response_format=response_format, structure=structure
    )
    with pytest.raises(ServiceValidationError, match="does not support.*JSON Schema"):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            call
        )
    assert native.entity.tasks == [] and list(native.media.iterdir()) == []


@pytest.mark.parametrize("result", [None, {}, ["text"], 42, "", " \n"])
async def test_invalid_native_response_cleans_files(native, frames, result):
    native.entity.result = result
    with pytest.raises(ServiceValidationError, match="invalid or empty text"):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            make_call(native.hass, frames)
        )
    assert list(native.media.iterdir()) == []


@pytest.mark.parametrize(
    "entity_id,state,features,error",
    [
        ("sensor.invalid", "unknown", 3, "invalid_entity"),
        ("ai_task.missing", None, 3, "missing_entity"),
        ("ai_task.test", "unavailable", 3, "unavailable"),
        ("ai_task.test", "unknown", 0, "missing_features"),
        ("ai_task.test", "unknown", 1, "missing_features"),
        ("ai_task.test", "unknown", 2, "missing_features"),
        ("ai_task.test", "unknown", None, "missing_features"),
    ],
)
async def test_runtime_validation(native, frames, entity_id, state, features, error):
    if state is not None:
        native.hass.states.async_set(entity_id, state, {"supported_features": features})
    with pytest.raises(ServiceValidationError, match=error):
        await HomeAssistantAITask(native.hass, entity_id).vision_request(
            make_call(native.hass, frames)
        )
    assert native.entity.tasks == []


async def test_entity_becomes_unavailable_after_config(native, frames):
    provider = HomeAssistantAITask(native.hass, native.entity.entity_id)
    await provider.validate()
    native.hass.states.async_set(
        native.entity.entity_id, "unavailable", {"supported_features": 3}
    )
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await provider.vision_request(make_call(native.hass, frames))
    assert list(native.media.iterdir()) == []


@pytest.mark.parametrize(
    "failure", [RuntimeError("synthetic failure"), TimeoutError("synthetic timeout")]
)
async def test_model_exception_cleanup(native, frames, failure):
    async def fail(task):
        raise failure

    native.entity.handler = fail
    with pytest.raises(type(failure), match=str(failure)):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            make_call(native.hass, frames)
        )
    assert list(native.media.iterdir()) == []


async def test_timeout_and_model_cancellation_cleanup(native, frames):
    entered = asyncio.Event()

    async def block(task):
        entered.set()
        await asyncio.Future()

    native.entity.handler = block
    provider = HomeAssistantAITask(native.hass, native.entity.entity_id)
    provider.request_timeout = 0.05
    with pytest.raises(TimeoutError):
        await provider.vision_request(make_call(native.hass, frames))
    assert entered.is_set() and list(native.media.iterdir()) == []
    entered.clear()
    provider.request_timeout = 60
    job = asyncio.create_task(provider.vision_request(make_call(native.hass, frames)))
    await asyncio.wait_for(entered.wait(), 2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert list(native.media.iterdir()) == []


async def test_cancellation_waits_for_executor_writer(native, frames, monkeypatch):
    """Cancellation must not orphan files created later by a running thread."""
    provider = HomeAssistantAITask(native.hass, native.entity.entity_id)
    entered = asyncio.Event()
    release = threading.Event()
    original = provider._write_attachments

    def slow_writer(images):
        native.hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(images)

    monkeypatch.setattr(provider, "_write_attachments", slow_writer)
    job = asyncio.create_task(provider.vision_request(make_call(native.hass, frames)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        job.cancel()
        await asyncio.sleep(0)
        # Exercise a second cancellation while the finally block waits for I/O.
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert native.entity.tasks == []
    assert list(native.media.iterdir()) == []


async def test_concurrent_requests_have_independent_files(native, frames):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block(task):
        if len(native.entity.tasks) == 2:
            entered.set()
        await release.wait()
        return GenDataTaskResult(conversation_id="synthetic", data="ok")

    native.entity.handler = block
    provider = HomeAssistantAITask(native.hass, native.entity.entity_id)
    jobs = [
        asyncio.create_task(
            provider.vision_request(make_call(native.hass, frames, message=name))
        )
        for name in ("first", "second")
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        paths = [
            {
                a.path
                for task in native.entity.tasks
                if task.instructions.endswith(name)
                for a in task.attachments
            }
            for name in ("first", "second")
        ]
        assert paths[0].isdisjoint(paths[1])
        jobs[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await jobs[0]
        assert all(not p.exists() for p in paths[0])
        assert all(p.exists() for p in paths[1])
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
    assert list(native.media.iterdir()) == []


async def test_partial_write_failure_cleanup(native, frames):
    with pytest.raises(ValueError):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            make_call(native.hass, [frames[0], "invalid-base64!"])
        )
    assert list(native.media.iterdir()) == []


@pytest.mark.parametrize("location", ["none", "www", "symlink"])
async def test_no_public_or_missing_media_directory(native, frames, location):
    public = native.tmp_path / "www"
    public.mkdir()
    if location == "symlink":
        alias = native.tmp_path / "alias"
        alias.symlink_to(public, target_is_directory=True)
        native.hass.config.media_dirs = {"local": str(alias)}
    else:
        native.hass.config.media_dirs = (
            {} if location == "none" else {"local": str(public)}
        )
    with pytest.raises(ServiceValidationError, match="private media_dirs"):
        await HomeAssistantAITask(native.hass, native.entity.entity_id).vision_request(
            make_call(native.hass, frames)
        )
    assert list(public.iterdir()) == []


async def test_existing_request_fallback_contract(native, frames):
    native.hass.data[DOMAIN]["fallback"] = {
        CONF_PROVIDER: "OpenAI",
        "api_key": "synthetic",
    }
    settings = native.hass.config_entries.async_entries(DOMAIN)[0]
    native.hass.config_entries.async_update_entry(
        settings, data={**settings.data, "fallback_provider": "fallback"}
    )
    native.hass.states.async_set(
        native.entity.entity_id, "unavailable", {"supported_features": 3}
    )
    req = Request(native.hass, "m", 10, 0.5)
    for image in frames:
        req.add_frame(image, "")
    with patch.object(
        OpenAI, "vision_request", AsyncMock(return_value="fallback result")
    ):
        assert await req.call(make_call(native.hass)) == {
            "response_text": "fallback result"
        }


async def test_config_and_reconfigure_native_selector(native):
    flow = llmvisionConfigFlow()
    flow.hass = native.hass
    flow.context = {"source": "user"}
    providers = await flow.async_step_user()
    assert (
        PROVIDER_AI_TASK
        in next(iter(providers["data_schema"].schema.values())).config["options"]
    )
    form = await flow.handle_provider(PROVIDER_AI_TASK)
    schema = form["data_schema"]
    assert len(schema.schema) == 1
    entity_selector = next(iter(schema.schema.values()))
    assert entity_selector.config["filter"] == [
        {"domain": ["ai_task"], "supported_features": [3]}
    ]
    data = {CONF_AI_TASK_ENTITY: native.entity.entity_id}
    assert schema(data) == data
    created = await flow.async_step_ai_task(data)
    assert created["data"] == {CONF_PROVIDER: PROVIDER_AI_TASK, **data}
    assert native.entity.tasks == []

    entry = MockConfigEntry(domain=DOMAIN, data=created["data"])
    entry.add_to_hass(native.hass)
    flow = llmvisionConfigFlow()
    flow.hass = native.hass
    flow.context = {"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    form = await flow.async_step_reconfigure(None)
    key = next(iter(form["data_schema"].schema))
    assert key.description["suggested_value"] == native.entity.entity_id
    # A second eligible entity is enough for local config validation, no model call.
    native.hass.states.async_set("ai_task.second", "unknown", {"supported_features": 3})
    with patch.object(
        native.hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ):
        updated = await flow.async_step_ai_task({CONF_AI_TASK_ENTITY: "ai_task.second"})
        await native.hass.async_block_till_done()
    assert updated["type"] == "abort" and updated["reason"] == "reconfigure_successful"
    assert entry.data[CONF_AI_TASK_ENTITY] == "ai_task.second"
    assert await async_setup_entry(native.hass, entry)
    assert (
        native.hass.data[DOMAIN][entry.entry_id][CONF_AI_TASK_ENTITY]
        == "ai_task.second"
    )
    assert native.entity.tasks == []


async def test_config_invalid_selection_error(native):
    flow = llmvisionConfigFlow()
    flow.hass = native.hass
    flow.context = {"source": "user"}
    form = await flow.async_step_ai_task({CONF_AI_TASK_ENTITY: "ai_task.missing"})
    assert form["errors"] == {CONF_AI_TASK_ENTITY: "ai_task_missing_entity"}


@pytest.mark.parametrize(
    "service,method,prefix",
    [
        ("image_analyzer", "add_images", ""),
        (
            "video_analyzer",
            "add_videos",
            "The attached images are frames from a video. ",
        ),
        (
            "stream_analyzer",
            "add_streams",
            "The attached images are frames from a live camera feed. ",
        ),
        (
            "data_analyzer",
            "add_visual_data",
            "Your job is to extract data from images.",
        ),
    ],
)
async def test_analyzer_handlers_reach_native_ai_task(
    native, frames, service, method, prefix
):
    """Only media capture is replaced; request, memory and native AI Task are real."""
    native.hass.http = Mock()
    assert await native.hass.async_add_executor_job(setup, native.hass, {})

    class PreparedMedia:
        key_frame = ""

        def __init__(self, hass, request):
            self.request = request

        async def prepare(self, **kwargs):
            for index, image in enumerate(frames):
                self.request.add_frame(image, f"synthetic-{index}.jpg")
            return self.request

    setattr(PreparedMedia, method, PreparedMedia.prepare)
    native.entity.result = "7"
    native.hass.states.async_set("input_number.synthetic", "0")
    with patch("custom_components.llmvision.MediaProcessor", PreparedMedia):
        result = await native.hass.services.async_call(
            DOMAIN,
            service,
            {
                "provider": "adapter",
                "message": "Count objects",
                "max_frames": 5,
                "model": "glimpse-v1",
                "sensor_entity": "input_number.synthetic",
            },
            blocking=True,
            return_response=True,
        )
    assert result["response_text"] == "7"
    assert native.entity.contents[0] == [base64.b64decode(image) for image in frames]
    assert "Count objects" in native.entity.tasks[0].instructions
    assert prefix in native.entity.tasks[0].instructions
    if service == "data_analyzer":
        assert native.hass.states.get("input_number.synthetic").state == "7.0"
        assert "extract only the requested info" in native.entity.tasks[0].instructions
    assert list(native.media.iterdir()) == []
