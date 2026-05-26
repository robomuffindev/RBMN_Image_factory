/* Robomuffin Image Factory — page-level client glue.
 * Every method log-traces with [robomuffin] so you can follow along in DevTools.
 */

console.log("[robomuffin] factory.js loaded at", new Date().toISOString(),
            "— Alpine ready:", !!window.Alpine);

function _log(...args) { console.log("[robomuffin]", ...args); }
function _err(...args) { console.error("[robomuffin]", ...args); }

document.addEventListener('alpine:init', () => {
  _log("alpine:init fired — registering projectPage component");

  Alpine.data('projectPage', (projectId) => ({
    tab: 'gallery',
    projectId,
    images: [],
    imagesById: {},
    counts: { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0, total: 0 },
    etaText: '',
    percentDone: 0,
    setupBanner: false,
    setupBannerHtml: '',
    newImage: { prompt: '', width: 1024, height: 1024, seed: '', reference_paths: [] },
    enhancing: false,
    generating: false,
    batch: { rows: [], filename: '' },
    batchCommitMode: 'append',
    batchProjectName: '',
    enhanceBulkRunning: false,
    sse: null,
    _firstDoneAt: null,
    _toastTimer: null,

    async init() {
      _log("projectPage.init() for project", this.projectId);
      const u = new URL(window.location.href);
      const t = u.searchParams.get('tab'); if (t) this.tab = t;
      await this.refreshSettingsFromServer();
      await this.refreshImages();
      this.subscribeSSE();
      _log("projectPage.init() done — images:", this.images.length,
           "tab:", this.tab, "setupBanner:", this.setupBanner);
    },

    setTab(t) {
      _log("setTab", t);
      this.tab = t;
      const u = new URL(window.location.href);
      u.searchParams.set('tab', t);
      window.history.replaceState({}, '', u.toString());
    },

    toast(msg, kind) {
      _log("toast", kind || "info", msg);
      let el = document.getElementById('rb-toast');
      if (!el) {
        el = document.createElement('div');
        el.id = 'rb-toast';
        el.style.cssText = 'position:fixed;bottom:1.5rem;right:1.5rem;z-index:60;padding:0.75rem 1rem;border-radius:0.5rem;font-size:0.875rem;color:#fff;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:opacity 0.2s';
        document.body.appendChild(el);
      }
      el.textContent = msg;
      el.style.background = kind === 'error' ? '#dc2626' : (kind === 'warn' ? '#d97706' : '#16a34a');
      el.style.opacity = '1';
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 3500);
    },

    async refreshSettingsFromServer() {
      try {
        const [sr, wr] = await Promise.all([
          fetch('/api/settings').then(r => r.json()),
          fetch('/api/workers').then(r => r.json()),
        ]);
        const issues = [];
        const urls = sr.comfyui_urls || [];
        if (!urls.length) {
          issues.push('No ComfyUI server URLs configured.');
        } else {
          const healthy = (wr.workers || []).filter(w => w.healthy).length;
          if (healthy === 0) issues.push('ComfyUI URL configured but no server is reachable (' + urls.length + ' configured, 0 healthy).');
        }
        const hasLLM = sr.default_llm_provider || sr.openai_api_key || sr.anthropic_api_key || sr.gemini_api_key || sr.ollama_base_url;
        if (!hasLLM) issues.push("No LLM provider configured — prompt enhancement won't work (generation still works).");
        this.setupBanner = issues.length > 0;
        this.setupBannerHtml = issues.map(s => '<li>' + s + '</li>').join('');
        _log("refreshSettingsFromServer — issues:", issues);
      } catch (e) { _err("refreshSettingsFromServer failed", e); }
    },

    async refreshImages() {
      try {
        const res = await fetch('/api/projects/' + this.projectId + '/images');
        if (!res.ok) { _err("refreshImages non-ok", res.status); return; }
        const data = await res.json();
        this.absorbImages(data.items || []);
        _log("refreshImages — got", data.items.length, "images");
      } catch (e) { _err("refreshImages error", e); }
    },

    absorbImages(arr) {
      this.images = arr;
      this.imagesById = {};
      for (const img of arr) this.imagesById[img.id] = img;
      this.recompute();
    },

    recompute() {
      const c = { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0, total: this.images.length };
      for (const img of this.images) if (c[img.status] !== undefined) c[img.status]++;
      this.counts = c;
      this.percentDone = c.total ? Math.round((c.done / c.total) * 100) : 0;
      const remaining = c.queued + c.running;
      if (remaining > 0 && c.done > 0 && this._firstDoneAt) {
        const elapsed = (Date.now() - this._firstDoneAt) / 1000;
        const rate = c.done / elapsed;
        if (rate > 0) this.etaText = '~' + Math.round(remaining / rate) + 's remaining';
      } else if (remaining === 0 && c.total > 0) {
        this.etaText = 'complete';
      } else this.etaText = '';
    },

    get canDownloadAll() { return this.counts.done > 0; },

    async uploadReference(ev) {
      const f = ev.target.files[0]; if (!f) return;
      _log("uploadReference", f.name, f.size, "bytes");
      const fd = new FormData(); fd.append('file', f);
      const res = await fetch('/api/projects/' + this.projectId + '/upload-reference', { method: 'POST', body: fd });
      if (res.ok) {
        const data = await res.json();
        this.newImage.reference_paths.push(data.path);
        this.toast('Uploaded ' + data.name);
      } else {
        this.toast('Upload failed', 'error');
      }
      ev.target.value = '';
    },

    async enhancePrompt() {
      _log("enhancePrompt — prompt length", this.newImage.prompt.length);
      this.enhancing = true;
      try {
        const res = await fetch('/api/llm/enhance', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ prompt: this.newImage.prompt, project_id: this.projectId })
        });
        if (res.ok) {
          const data = await res.json();
          if (data.enhanced) {
            this.newImage.prompt = data.enhanced;
            this.toast('Enhanced (' + data.provider + '/' + data.model + ')');
          }
        } else {
          const txt = await res.text();
          _err("enhancePrompt failed", res.status, txt);
          this.toast('Enhance failed: ' + txt.slice(0, 120), 'error');
        }
      } catch (e) { _err("enhancePrompt error", e); this.toast('Enhance error: ' + e, 'error'); }
      finally { this.enhancing = false; }
    },

    async generate() {
      _log("generate — prompt:", this.newImage.prompt.slice(0, 60),
           "size:", this.newImage.width + 'x' + this.newImage.height,
           "refs:", this.newImage.reference_paths.length);
      this.generating = true;
      try {
        const seed = (this.newImage.seed === '' || this.newImage.seed === 'random') ? null : parseInt(this.newImage.seed, 10);
        const res = await fetch('/api/projects/' + this.projectId + '/images', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            prompt: this.newImage.prompt,
            width: this.newImage.width, height: this.newImage.height, seed,
            reference_paths: this.newImage.reference_paths, dispatch: true
          })
        });
        if (res.ok) {
          const img = await res.json();
          this.images.unshift(img);
          this.imagesById[img.id] = img;
          this.recompute();
          this.newImage.prompt = '';
          this.newImage.reference_paths = [];
          this.newImage.seed = '';
          this.toast('Queued — watch the card for progress.');
          _log("generate queued image", img.id);
        } else {
          const txt = await res.text();
          _err("generate failed", res.status, txt);
          this.toast('Generate failed: ' + txt.slice(0, 120), 'error');
        }
      } catch (e) { _err("generate error", e); this.toast('Generate error: ' + e, 'error'); }
      finally { this.generating = false; }
    },

    async rerun(img) {
      _log("rerun", img.id);
      const res = await fetch('/api/images/' + img.id + '/dispatch', { method: 'POST' });
      if (res.ok) {
        img.status = 'queued'; img.output_path = null; img.error = null; img.progress = 0;
        this.recompute();
        this.toast('Re-queued');
      } else {
        this.toast('Re-run failed', 'error');
      }
    },

    async deleteImage(img) {
      if (!confirm('Delete this image?')) return;
      _log("deleteImage", img.id);
      const res = await fetch('/api/images/' + img.id, { method: 'DELETE' });
      if (res.ok) {
        this.images = this.images.filter(i => i.id !== img.id);
        delete this.imagesById[img.id];
        this.recompute();
        this.toast('Deleted');
      } else { this.toast('Delete failed', 'error'); }
    },

    downloadZip() { window.location = '/api/projects/' + this.projectId + '/zip?include_prompts=1'; },

    async deleteProject() {
      if (!confirm('Delete this project AND all its files?')) return;
      _log("deleteProject", this.projectId);
      const res = await fetch('/api/projects/' + this.projectId, { method: 'DELETE' });
      if (res.ok) window.location = '/';
      else this.toast('Delete project failed', 'error');
    },

    openLightbox(d) { this._lb = d; window.dispatchEvent(new CustomEvent('lightbox-open', { detail: d })); },

    async loadBatchFile(ev) {
      const f = ev.target.files[0]; if (!f) return;
      _log("loadBatchFile", f.name);
      const fd = new FormData(); fd.append('file', f);
      const res = await fetch('/api/batch/preview', { method: 'POST', body: fd });
      if (!res.ok) { this.toast('Parse failed: ' + (await res.text()).slice(0,120), 'error'); return; }
      const data = await res.json();
      this.batch.rows = data.rows || [];
      this.batch.filename = f.name;
      this.batchProjectName = data.project_name || '';
      this.toast('Parsed ' + data.rows.length + ' rows');
      ev.target.value = '';
    },

    get canCommitBatch() {
      if (!this.batch.rows.length) return false;
      if (this.batchCommitMode === 'new' && !this.batchProjectName) return false;
      return true;
    },

    async commitBatch() {
      _log("commitBatch mode=" + this.batchCommitMode, "rows=" + this.batch.rows.length);
      const payload = {
        mode: this.batchCommitMode,
        project_id: this.batchCommitMode === 'append' ? this.projectId : null,
        project_name: this.batchProjectName,
        rows: this.batch.rows
      };
      const res = await fetch('/api/batch/commit', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      if (!res.ok) { this.toast('Commit failed: ' + (await res.text()).slice(0,120), 'error'); return; }
      const data = await res.json();
      this.toast('Queued ' + data.queued + ' jobs');
      if (data.project_id && data.project_id !== this.projectId) {
        window.location = '/projects/' + data.project_id + '?tab=results';
      } else {
        this.tab = 'results';
        this.batch.rows = [];
        await this.refreshImages();
      }
    },

    async enhanceAllInBatch() {
      this.enhanceBulkRunning = true;
      try {
        const prompts = this.batch.rows.map(r => r.prompt);
        const res = await fetch('/api/llm/enhance-bulk', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ prompts, project_id: this.projectId })
        });
        if (res.ok) {
          const data = await res.json();
          (data.results || []).forEach((r, i) => { if (r.enhanced) this.batch.rows[i].prompt = r.enhanced; });
          this.toast('Enhanced ' + data.results.length + ' prompts');
        } else { this.toast('Bulk enhance failed', 'error'); }
      } finally { this.enhanceBulkRunning = false; }
    },

    subscribeSSE() {
      try {
        const es = new EventSource('/api/jobs/stream');
        _log("SSE subscribed to /api/jobs/stream");
        const update = (data) => {
          if (!data || !data.image_id) return;
          const existing = this.imagesById[data.image_id];
          if (existing) {
            if (data.status) existing.status = data.status;
            if (data.progress != null) existing.progress = data.progress;
            if (data.error) existing.error = data.error;
            if (data.status === 'done' || data.status === 'failed') {
              if (!this._firstDoneAt && data.status === 'done') this._firstDoneAt = Date.now();
              this.refreshImages();
            }
            this.recompute();
          } else { this.refreshImages(); }
        };
        es.addEventListener('job_started',   e => update(JSON.parse(e.data)));
        es.addEventListener('job_progress',  e => update(JSON.parse(e.data)));
        es.addEventListener('job_completed', e => update(JSON.parse(e.data)));
        es.addEventListener('job_failed',    e => update(JSON.parse(e.data)));
        es.addEventListener('job_retrying',  e => update(JSON.parse(e.data)));
        es.onerror = (e) => _err("SSE error", e);
        this.sse = es;
      } catch (e) { _err("subscribeSSE failed", e); }
    },
  }));

  _log("projectPage Alpine.data registered");
});
