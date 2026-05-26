# Frontend Patterns — Architecture Reference

## Tech Stack

- React 18 + TypeScript + Vite
- TailwindCSS for styling
- Zustand for global state
- @tanstack/react-query for server state
- wavesurfer.js for audio waveforms
- Axios for HTTP
- Native EventSource for SSE

---

## API Client (`frontend/src/api/client.ts`)

### Setup

```typescript
const api = axios.create({ baseURL: '/api', headers: { 'Content-Type': 'application/json' } });
```

All functions return Axios response promises. Types are applied via generics: `api.get<Project>(...)`.

### Complete Function List

#### Projects
| Function | Method | Route |
|----------|--------|-------|
| `createProject(data)` | POST | `/projects` |
| `getProjects()` | GET | `/projects` |
| `getProject(id)` | GET | `/projects/:id` |
| `updateProject(id, data)` | PUT | `/projects/:id` |
| `deleteProject(id)` | DELETE | `/projects/:id` |
| `duplicateProject(id)` | POST | `/projects/:id/duplicate` |

#### Scenes
| Function | Method | Route |
|----------|--------|-------|
| `getScenes(pid)` | GET | `/projects/:pid/scenes` |
| `createScene(pid, data)` | POST | `/projects/:pid/scenes` |
| `getScene(pid, sid)` | GET | `/projects/:pid/scenes/:sid` |
| `updateScene(pid, sid, data)` | PUT | `/projects/:pid/scenes/:sid` |
| `deleteScene(pid, sid)` | DELETE | `/projects/:pid/scenes/:sid` |
| `reorderScenes(pid, order)` | PUT | `/projects/:pid/scenes/reorder` |
| `setSceneStems(pid, sid, stems)` | POST | `/projects/:pid/scenes/:sid/stems` |
| `getPrevSceneLastFrame(pid, sid)` | GET | `/projects/:pid/scenes/:sid/prev-last-frame` |
| `uploadSceneMedia(pid, sid, formData)` | POST | `/projects/:pid/scenes/:sid/upload` |

#### Generation
| Function | Method | Route | Notes |
|----------|--------|-------|-------|
| `generateImage(pid, req)` | POST | `/projects/:pid/generate/image` | |
| `generateVideo(pid, req)` | POST | `/projects/:pid/generate/video` | |
| `enhancePrompt(pid, req)` | POST | `/projects/:pid/generate/enhance-prompt` | |
| `rerunPass2(pid, req)` | POST | `/projects/:pid/generate/rerun-pass2` | |
| `batchGenerate(pid, jobs)` | POST | `/projects/:pid/generate/batch` | |
| `autoGenerate(pid, mode)` | POST | `/projects/:pid/generate/auto` | timeout: 300s |
| `startSequentialAutoGen(pid, req)` | POST | `/projects/:pid/generate/auto-sequential` | |
| `getSequentialAutoGenStatus(pid)` | GET | `/projects/:pid/generate/auto-sequential/status` | |
| `cancelSequentialAutoGen(pid)` | POST | `/projects/:pid/generate/auto-sequential/cancel` | |
| `generateTransition(pid, req)` | POST | `/projects/:pid/generate/transition` | |

#### Timeline & Audio
| Function | Method | Route | Notes |
|----------|--------|-------|-------|
| `analyzeAudio(pid, formData)` | POST | `/projects/:pid/timeline/analyze` | timeout: 0 (Demucs) |
| `getSections(pid)` | GET | `/projects/:pid/timeline/sections` | |
| `getLyrics(pid)` | GET | `/projects/:pid/timeline/lyrics` | |
| `rerunWhisper(pid)` | POST | `/projects/:pid/timeline/rerun-whisper` | timeout: 0 |
| `suggestTimeline(pid)` | POST | `/projects/:pid/timeline/suggest-timeline` | timeout: 180s |
| `sliceSceneAudio(pid)` | POST | `/projects/:pid/timeline/slice-audio` | |

#### Settings
| Function | Method | Route |
|----------|--------|-------|
| `getSettings()` | GET | `/settings` |
| `updateSettings(data)` | PUT | `/settings` |
| `testComfyUI({url})` | POST | `/settings/test-comfyui` |
| `testWhisper()` | POST | `/settings/test-whisper` |
| `testLLM({provider, api_key, model})` | POST | `/settings/test-llm` |
| `exportSettings()` | GET | `/settings/export` |
| `importSettings(formData)` | POST | `/settings/import` |
| `getBuiltinPrompt(model, type)` | GET | `/settings/builtin-prompt` |

#### Concept & Video Flow
| Function | Method | Route |
|----------|--------|-------|
| `getConcept(pid)` | GET | `/projects/:pid/concept` |
| `saveConcept(pid, data)` | PUT | `/projects/:pid/concept` |
| `baseOnLyrics(pid, data)` | POST | `/projects/:pid/concept/base-on-lyrics` |
| `generateVideoFlow(pid)` | POST | `/projects/:pid/concept/flow/generate` |
| `updateSceneFlow(pid, sid, idea)` | PUT | `/projects/:pid/concept/flow/:sid` |

#### Jobs
| Function | Method | Route |
|----------|--------|-------|
| `getJobs(params)` | GET | `/jobs` |
| `cancelJob(id)` | POST | `/jobs/:id/cancel` |
| `retryJob(id)` | POST | `/jobs/:id/retry` |
| `deleteJob(id)` | DELETE | `/jobs/:id` |
| `purgeJobs()` | POST | `/jobs/purge` |

#### Export
| Function | Method | Route |
|----------|--------|-------|
| `exportVideo(pid, config)` | POST | `/projects/:pid/export` |
| `getExportStatus(pid)` | GET | `/projects/:pid/export/status` |
| `renderPreview(pid)` | POST | `/projects/:pid/export/preview` |
| `getPreviewStatus(pid)` | GET | `/projects/:pid/export/preview` |

---

## TypeScript Types (`frontend/src/types/index.ts`)

### Key Interfaces

```typescript
interface Project {
    id: string; name: string; mode: ProjectMode;
    settings: Record<string, any>;  // generic bag for project-level settings
    schema_version: number;
    scene_count: number; asset_count: number; project_path: string;
}

interface Scene {
    id: string; project_id: string; order_index: number;
    name: string; start_time: number; end_time: number;
    prompt: string; negative_prompt: string;
    parameters: Record<string, any>;  // flow_idea, lyrics, two_pass prompts, seeds, etc.
    workflow_snapshot: Record<string, any>;
    stem_selection: StemSelection;
    generation_history: GenerationHistory[];
}

interface Job {
    id: string; project_id: string; scene_id?: string;
    job_type: JobType; status: JobStatus; priority: number;
    worker_url?: string; prompt_id?: string;
    parameters: Record<string, any>; result: Record<string, any>;
    error?: string; retry_count: number;
    // UI-only (from SSE):
    progress?: number; current_node?: string;
}

interface AppSettings {
    // 40+ fields — see 03-settings-api-architecture.md for complete list
    comfyui_urls: string[];
    openai_api_key?: string; openai_model?: string;
    anthropic_api_key?: string; anthropic_model?: string;
    gemini_api_key?: string; gemini_model?: string;
    default_llm_provider?: string;
    image_model_type: string; video_model_type: string;
    image_system_prompt_overrides?: Record<string, SystemPromptOverrideEntry>;
    video_system_prompt_overrides?: Record<string, SystemPromptOverrideEntry>;
    // ... etc
}

interface WorkflowConfig {
    id: string; name: string;
    workflow_type: 'image' | 'video';
    is_default: boolean; server_url?: string;
    workflow_json: Record<string, any>;
    field_mappings: WorkflowFieldMapping[];
}
```

### Enums (string unions)

```typescript
type ProjectMode = 'music_video' | 'narration_images' | 'narration_video';
type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'retrying' | 'cancelled';
type JobType = 'image' | 'video';
type ImageWorkflowType = 'klein_1ref' | 'klein_2ref' | 'klein_3ref' | 'klein_4ref' | 'klein_t2i' | 'custom';
type VideoWorkflowType = 'ltx_fflf' | 'ltx_i2v' | 'ltx_v2v_extend' | 'custom';
```

---

## Zustand Store (`frontend/src/store/index.ts`)

### Shape

```typescript
interface AppState {
    // Server data (cached from react-query)
    currentProject: Project | null;
    scenes: Scene[];
    sections: SongSection[];
    assets: Asset[];
    jobs: Job[];
    workflows: WorkflowConfig[];

    // UI state
    activeScene: Scene | null;
    playbackPosition: number;
    isPlaying: boolean;
    viewMode: 'sections' | 'scenes';
    selectedSectionId: string | null;
    scenesLocked: boolean;

    // Actions
    setProject, setScenes, setSections, setAssets, setWorkflows, setJobs
    setActiveScene, setPlaybackPosition, togglePlay, setIsPlaying
    setViewMode, setSelectedSectionId, setScenesLocked
    addJob, updateJob, removeJob
    updateSceneInStore, addScene, removeScene
    addAsset, removeAsset
}
```

### Key Pattern

The store is a **thin synchronization layer**. It does NOT own async data fetching.

- **react-query** owns server state (fetching, caching, invalidation)
- **Zustand** caches it for cross-component reads + handles UI-only state
- `updateSceneInStore` keeps `activeScene` in sync with the `scenes` array

---

## react-query Patterns

### Query Keys

```typescript
['project', id]      // single project
['projects']         // project list
['scenes', id]       // scenes for project
['sections', id]     // sections for project
['assets', id]       // assets for project
['settings']         // app settings
['workflows']        // workflow configs
['jobs']             // job list
['concept', id]      // concept data
['lyrics', id]       // lyrics data
```

### Mutation Pattern

```typescript
const mutation = useMutation({
    mutationFn: async (data) => {
        const res = await apiClient.doThing(projectId, data);
        return res.data;
    },
    onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: ['scenes', projectId] });
        // Optional: update Zustand store directly for immediate UI
    },
});
```

### SSE-Triggered Invalidation

When SSE `job_completed` arrives, the handler invalidates relevant queries:
```typescript
queryClient.invalidateQueries({ queryKey: ['scenes', projectId] });
queryClient.invalidateQueries({ queryKey: ['jobs'] });
```

---

## Settings Page UI Patterns

### State Management

```typescript
// Local state for entire settings object — NOT Zustand
const [settings, setSettings] = useState<AppSettings>(defaults);

// Sync from server on load
const { data: savedSettings } = useQuery({ queryKey: ['settings'], queryFn: getSettings });
useEffect(() => {
    if (savedSettings) setSettings({ ...defaults, ...savedSettings });
}, [savedSettings]);

// Single save button PUTs entire object
const saveMutation = useMutation({
    mutationFn: () => updateSettings(settings),
    onSuccess: (res) => setSettings(res.data),
});
```

### Layout Pattern

```
┌─ Header (Back + Title) ─────────────────────────────────┐
├─ Sticky Save Bar (Cancel + Save) ───── sticky top-0 z-30 ┤
│                                                           │
│  ┌─ Section Card ────── bg-gray-900 rounded-lg p-6 ────┐ │
│  │  <h2> Section Title                                   │ │
│  │  <p> Description text (text-gray-400)                 │ │
│  │  Form controls...                                     │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                           │
│  ┌─ Section Card ────────────────────────────────────────┐│
│  │  ...                                                   ││
│  └──────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

Container: `max-w-3xl mx-auto p-8 space-y-8`

### Per-Model Keying Pattern

Settings keyed by model name (prompts, guidance):

```typescript
// Getter
const getImageOverride = (modelName: string): SystemPromptOverrideEntry =>
    settings.image_system_prompt_overrides?.[modelName] ?? { text: '', enabled: false };

// Setter
const setImageOverride = (modelName: string, entry: SystemPromptOverrideEntry) =>
    setSettings(prev => ({
        ...prev,
        image_system_prompt_overrides: {
            ...prev.image_system_prompt_overrides,
            [modelName]: entry,
        },
    }));
```

### Test Connection Pattern

```typescript
const [testResults, setTestResults] = useState<Record<string, { success: boolean; message?: string }>>({});

const testComfyMutation = useMutation({
    mutationFn: (url: string) => testComfyUI({ url }),
    onSuccess: (res, url) => setTestResults(prev => ({ ...prev, [url]: { success: true } })),
    onError: (err, url) => setTestResults(prev => ({ ...prev, [url]: { success: false, message: err.message } })),
});
```

Rendered as check/X icons next to each server URL.

---

## SSE Event Consumption

```typescript
export function subscribeToJobEvents(onUpdate: (event: JobEvent) => void): () => void {
    const es = new EventSource('/api/jobs/stream');

    es.addEventListener('job_started', (e) => {
        const data = JSON.parse(e.data);
        onUpdate({ type: 'started', job: data });
    });

    es.addEventListener('job_progress', (e) => {
        const data = JSON.parse(e.data);
        onUpdate({ type: 'progress', jobId: data.job_id, progress: data.progress });
    });

    es.addEventListener('job_completed', (e) => {
        const data = JSON.parse(e.data);
        onUpdate({ type: 'completed', job: data });
    });

    es.addEventListener('job_failed', (e) => {
        const data = JSON.parse(e.data);
        onUpdate({ type: 'failed', jobId: data.job_id, error: data.error });
    });

    return () => es.close();
}
```

Auto-reconnect handled by browser's native EventSource behavior.

---

## Patterns for Adding New Features

### New API Endpoint

1. Add function to `client.ts`: `export const myFn = (params) => api.method<Type>('/route', data)`
2. Add types to `types/index.ts` if needed

### New Setting

1. Add field to `AppSettings` interface in types
2. Add default in SettingsPage `useState` and `useEffect` sync
3. Add UI control in appropriate `<section>`
4. Save button already PUTs entire object — no mutation changes needed

### New Real-Time Event

1. Backend: `job_event_broadcaster.put_nowait({"type": "my_event", ...})`
2. Frontend: Add `es.addEventListener('my_event', ...)` in `subscribeToJobEvents`
3. Handler: update Zustand store or invalidate react-query

### New Store Field

1. Add field + setter to `AppState` interface
2. Add to `create()` call
3. Simple setters for server data; map/filter for mutations

### Component Data Flow

```
Server Data:  react-query → Zustand store cache → component reads
Mutations:    component → API client → Zustand update / query invalidation
UI State:     Zustand for shared (active scene, playback)
              useState for component-scoped (modals, form inputs)
```
