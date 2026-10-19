# Home Assistant AI Task provider (independent fork)

## English

This fork adds **Home Assistant AI Task** to LLM Vision. It uses an existing
`ai_task.*` entity through HA's exported `ai_task.async_generate_data` Python API.
It works with any AI Task integration advertising both `GENERATE_DATA` and
`SUPPORT_ATTACHMENTS`, provided its configured model actually accepts images.
There is no provider-specific authentication, proxy, or additional runtime package.

### Install and configure

1. Back up your existing LLM Vision configuration. This is a replacement for the
   `llmvision` integration, not a second integration to install alongside it.
2. For an isolated test installation, download the feature branch from
   [Skriipt/ha-llmvision](https://github.com/Skriipt/ha-llmvision/tree/feature/ha-ai-task-provider)
   and copy `custom_components/llmvision` into your test HA configuration's
   `custom_components` directory. Load the changed integration by restarting that
   test instance. This branch is not a published HACS release.
3. Configure your chosen AI Task integration in HA first. Configure its model,
   credentials and sampling there. The entity must be available; an initial state
   of `unknown` is normal and accepted.
4. Ensure HA already has an existing, writable, private `homeassistant.media_dirs`
   directory (for example the normal `/media` directory). The adapter uses the first
   suitable configured directory, never creates a media mapping, and refuses paths
   inside HA's public `www` directory, including symlinks into it. Do not use a
   directory separately served by a public web server.
5. In **Settings → Devices & services → Add integration → LLM Vision**, complete
   the shared Settings entry if needed, then add **Home Assistant AI Task** and
   select the entity. There are no API-key or model fields and no model call is
   made during setup. Use **Reconfigure** on this provider entry to change entities.
6. Select the new **LLM Vision provider config entry** in existing image, stream,
   video or Data Analyzer actions. This is distinct from the `ai_task.*` entity
   selected in step 5. Use `response_format: text` and omit `structure`.

### Behavior and limits

- The existing LLM Vision frame selection and resizing remain in use. All prepared
  JPEGs are passed in order, with optional reference-memory images before analysis
  frames. There is no adapter limit of four images; native tests include five
  frames and two references plus five frames. Individual models may impose limits;
  their errors reach the existing provider fallback/error handling.
- HA requires local attachment paths. The adapter writes each request into its own
  random mode-0700 directory under the configured media directory and passes
  `media-source://media_source/...` IDs to HA's native resolver. It adds no HTTP
  endpoints, public `www` files, signed URLs or base64 data URLs. HA's existing
  media endpoint requires authentication. Files are removed after success, model
  errors, timeouts and cancellation, including cancellation while the executor is
  still writing. Concurrent requests have separate directories. Cleanup waits for
  outstanding disk I/O, so a timeout/cancellation can take longer than the model
  timeout. A process crash or power loss cannot execute cleanup; leftover
  `llmvision-*` directories may then need manual deletion.
- Custom LLM Vision system instructions, optional memory/reference labels, user
  instructions and text-only title calls are retained. AI Task exposes a single
  `instructions` string, so the adapter places these instructions there; it cannot
  override the AI Task integration's system role. Titles attach no images. Text
  results keep the existing `response_text`, title and timeline paths. Data Analyzer
  continues to use its extraction prompt and text result.
- **Model and sampling belong exclusively to the selected AI Task integration.**
  LLM Vision's `model`, `max_tokens`, `temperature`, `top_p`, thinking and similar
  provider options are not forwarded. In particular, a blueprint's `glimpse-v1` or
  Gemini model value cannot select a model or trigger Glimpse response processing.
  The stable internal routing value `home-assistant-ai-task` is not a model name.
  The shared LLM Vision request timeout does apply.
- **Structured output is unsupported by this adapter.** LLM Vision's JSON Schema
  is not HA's selector-based action structure (or the native compiled schema).
  `response_format: json` or a nonempty `structure` raises a clear error rather than
  sending the wrong format or promising validation. Use another LLM Vision provider
  for structured output. The existing fallback may handle this error if configured.
  Empty or non-string AI Task responses also raise an error.

### Verification and API references

The repository test environment pins **HA 2025.11.2 / Python 3.13**. Native tests use
real AI Task entities, chat logs and local media resolution; only model execution
is synthetic. The exported API and local attachment contract were also checked
against **HA Core 2026.10.2**. No live model/provider compatibility or production HA
installation is claimed by these offline tests. Use HA 2025.11.2 or newer for this
provider; the entity itself was introduced in 2025.7.

- [Official AI Task documentation](https://www.home-assistant.io/integrations/ai_task/)
- [Core 2026.10.2 AI Task API and attachment resolver](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/ai_task/task.py)
- [Core 2026.10.2 local media source](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/media_source/local_source.py)
- [Core 2025.11.2 tested API](https://github.com/home-assistant/core/blob/2025.11.2/homeassistant/components/ai_task/task.py)

Run locally without a running HA instance or credentials:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest tests/test_ai_task_provider.py -q
.venv/bin/python -m pytest tests/ --ignore=tests/test_api.py -m 'not integration' -q
```

The ignored legacy `test_api.py` talks to a separately configured HA instance and
must not be used against production for this patch. All new fixtures are synthetic.
GitHub Actions are disabled on this fork; validation is local.

## Deutsch

Der zusätzliche Anbieter **Home Assistant AI Task** verwendet eine bereits
eingerichtete `ai_task.*`-Entität. Die Entität muss Datengenerierung und Anhänge
unterstützen, das gewählte Modell muss Bilder verarbeiten können. Zugangsdaten,
Modell und Sampling bleiben in der jeweiligen AI-Task-Integration.

Installation zunächst in einer isolierten HA-Instanz: den Feature-Branch dieses
Forks herunterladen, dessen `custom_components/llmvision` in die Testinstanz
kopieren und diese neu starten. Die vorhandene Integration wird ersetzt; beide
Versionen können nicht gleichzeitig unter derselben Domain laufen. Vorher sichern.
Dies ist kein veröffentlichtes HACS-Release.

Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen → LLM Vision**
zuerst gegebenenfalls den gemeinsamen Settings-Eintrag einrichten. Danach den
Anbieter **Home Assistant AI Task** hinzufügen und eine passende Entität wählen.
Ein initialer Zustand `unknown` ist zulässig, `unavailable` wird abgelehnt.
**Neu konfigurieren** erlaubt einen späteren Entitätswechsel. Im Analyzer den neuen
LLM-Vision-Anbietereintrag auswählen, `response_format: text` verwenden und
`structure` weglassen. Einrichtung und Neukonfiguration rufen kein Modell auf.

Voraussetzungen: HA **2025.11.2 oder neuer**, eine eingerichtete bildfähige
AI-Task-Integration und ein vorhandenes, beschreibbares, privates HA-Medienverzeichnis
in `homeassistant.media_dirs`, beispielsweise `/media`. Der Adapter ändert diese
Konfiguration nicht. `www` und darauf zeigende Symlinks werden abgelehnt; das
Medienverzeichnis darf auch nicht anderweitig öffentlich bereitgestellt werden.
Die temporären JPEGs liegen pro Aufruf in einem eigenen Verzeichnis mit Modus 0700
und werden über den nativen HA-Medienpfad übergeben. Es entstehen keine neuen
Endpoints. Cleanup wartet auch bei Abbruch auf den Schreibthread und entfernt
anschließend dessen Dateien. Bei Prozessabsturz oder Stromausfall ist eine manuelle
Entfernung zurückgebliebener `llmvision-*`-Verzeichnisse nötig.

Bild-, Stream-, Video- und Data Analyzer, optionale Memory-Bilder, Texttitel und
Timeline verwenden ihre bisherigen Aufrufpfade. Der Adapter übergibt alle bereits
ausgewählten Frames in Reihenfolge; es gibt keine künstliche Vier-Bilder-Grenze.
Modellgrenzen können weiterhin Fehler auslösen. Benutzerdefinierte Prompts werden
als AI-Task-Anweisungen übergeben, nicht als eigener System-Rollenparameter.

LLM-Vision-Parameter wie `model`, `max_tokens`, `temperature` und `top_p` haben bei
diesem Anbieter keine Wirkung auf das Modell. Auch Blueprint-Modellwerte werden
ignoriert und lösen keine Glimpse-Nachverarbeitung aus. Der gemeinsame
Anfrage-Timeout gilt weiterhin. **JSON-Schema/strukturierte Ausgabe ist ausdrücklich
nicht unterstützt**; dafür einen anderen Anbieter verwenden. Die vorhandene
Fallback-Behandlung bleibt wirksam. Leere oder nicht als Text gelieferte Antworten
werden als Fehler behandelt.

Die lokalen Tests oben verwenden ausschließlich synthetische Bilder und den echten
HA-Attachment-Resolver. Sie prüfen keine produktive Instanz und keinen echten
Modellanbieter. Die produktive Home-Assistant-Konfiguration wurde für diesen Patch
nicht verändert.
