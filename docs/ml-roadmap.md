# Shoebox — photo-library ML roadmap

Shoebox is the cohort's chosen home for filling the **on-device photo
ML** gap in the Linux mobile world (see the larger Linux-mobile gap
inventory tracked in Claude's memory). The shape of that work is
layered, smallest dependency first.

## Phase 0 — Immich smart-search wiring · *shipped*

CLIP-based search lives server-side in Immich; the client just had to
surface it.

- `Backend.search_smart(query, *, limit=100) -> list[RemoteAsset]`
  in `backends/base.py`, opt-in (defaults to `NotImplementedError`)
  the same way `update_asset` is.
- `ImmichBackend.search_smart` posts to `/api/search/smart` and
  reuses `_asset_from_json` (the response shape matches
  `/search/metadata`).
- `Database.list_assets_by_remote_ids` maps server-ranked
  `remote_id`s back to local catalog rows, preserving order and
  silently skipping rows that aren't synced yet.
- `ui/search.py` + `data/ui/search.blp`: `SearchPage` with a
  debounced `Gtk.SearchEntry` (the entry's own `search-delay`),
  monotonic `_query_seq` to discard stale in-flight results, and a
  `Gtk.Stack` for prompt / loading / results / empty / error states.
- Search button in the gallery header pushes the page.

### Follow-ups visible from Phase 0

- **Capability flag on `Backend`.** Today the search button is always
  visible; non-Immich backends would surface `NotImplementedError`
  as "This backend does not support search". A cheap
  `Backend.supports_smart_search: bool` (or a more general
  `capabilities` set) lets the gallery hide the button instead.
- **Recovering unsynced hits.** Server hits with no local row are
  counted in the caption ("N not yet synced") but not rendered. A
  follow-up could fetch the `RemoteAsset` and either ingest it or
  build a transient `Asset` good enough for tile + detail.
- **Recent searches** in the entry's completion popup.

## Phase 1 — Local CLIP embeddings · *next*

Goal: smart search works **offline** and on **unsynced local
photos**. Backend-agnostic — useful even with PhotoPrism or a
no-server "library is just my disk" mode.

### Approach

- Embedding runtime: ONNX Runtime via `pip install onnxruntime`
  (CPU-only — phone GPUs aren't a realistic target), or `clip.cpp`
  if footprint matters more than ergonomics. ggml-style int8 ViT-B/32
  is ~80 MB and embeds an image in ~200–500 ms on a PinePhone-class
  CPU.
- Storage: new `assets_embedding` table keyed on `asset.id`, BLOB
  column for the float16/int8 vector + a `model_version` column so
  we can re-embed on upgrade. Vector dim matches Immich's CLIP so
  the two embedding sources stay interchangeable.
- Query: cosine similarity in Python over the cached vectors — fine
  up to ~100 k photos. If we outgrow that, swap in
  [`sqlite-vec`](https://github.com/asg017/sqlite-vec) without
  changing the storage layout.
- Scheduling: reuse `sync/conditions.py` (NetworkManager + UPower)
  with a new "embed-when-charging" toggle next to the existing
  Wi-Fi-only / unmetered gates. All inference goes through
  `worker.py`; progress posts via `GLib.idle_add`.
- UX: `SearchPage` already abstracts over "list of remote_ids" — a
  `LocalEmbedder` implementation feeds the same grid. Search results
  union server + local hits (dedupe by `asset.id`).

### Risks / things to watch

- **Flatpak deps multi-arch.** `onnxruntime` ships aarch64 wheels
  but pin versions explicitly and use `only-arches` in
  `fix-flatpak-deps.py` (this trap is logged in Claude memory).
- **Model packaging.** ~80 MB bundled is fine for Flathub; >200 MB
  draws reviewer questions. A first-run download into
  `$XDG_DATA_HOME/shoebox/models/` with hash verification is the
  cleaner path either way.
- **Battery realism.** Even gated to charging-only, indexing a
  20 k-photo library is hours of CPU. Surface progress, let users
  pause, persist progress across restarts.
- **CMake-ninja libdir trap** (logged in Claude memory) — applies
  if we end up vendoring `clip.cpp` instead of using a wheel.

## Phase 2 — Face clustering · *later*

The user-visible payoff. Heaviest of the three.

- Detection: SCRFD ONNX (~10 MB).
- Embedding: ArcFace ONNX (~25 MB).
- Clustering: DBSCAN/HDBSCAN. Persist clusters; let the user merge,
  split, name. Hardest part is the UX, not the ML.
- Storage: `faces` table (asset_id, bbox, embedding, cluster_id).
- New "People" navigation page reusing the existing tile factory.

## Non-goals

- Cloud ML services. The whole point is **on-device**.
- DRM-gated / attestation-gated features — out of scope for the
  cohort entirely (see linux-mobile-gaps memory).
