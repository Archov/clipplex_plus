(() => {
  'use strict';

  const dataNode = document.getElementById('clip_library_data');
  let clips = JSON.parse(dataNode ? dataNode.textContent : '[]');
  let refreshRequest = 0;
  const validSorts = ['newest', 'oldest', 'title_asc', 'title_desc', 'duration_asc', 'duration_desc'];
  const storedSort = localStorage.getItem('clipplex.librarySort');
  const state = {
    view: localStorage.getItem('clipplex.libraryView') === 'list' ? 'list' : 'grid',
    size: ['small', 'medium', 'large'].includes(localStorage.getItem('clipplex.librarySize')) ? localStorage.getItem('clipplex.librarySize') : 'medium',
    editingPath: null,
    deletingPath: null,
    uploadingPath: null,
    deleteTimer: null,
    searchTimer: null,
    uploaders: [],
    immichLoaded: false,
    collapsedLibraries: new Set(),
    sort: validSorts.includes(storedSort) ? storedSort : (dataNode?.dataset.sort || 'newest'),
  };

  const byId = id => document.getElementById(id);
  const libraryRoot = byId('library_groups');
  const filters = {
    search: byId('clip_search'), library: byId('library_filter'), type: byId('type_filter'),
    title: byId('title_filter'), episode: byId('episode_filter'),
  };

  function clipByPath(path) { return clips.find(clip => clip.file_path === path) || null; }
  function unique(values) { return Array.from(new Set(values.filter(Boolean))).sort((a, b) => a.localeCompare(b)); }
  function titleKey(clip) { return clip.media_type === 'episode' ? clip.show || 'Unknown Series' : clip.title || 'Untitled movie'; }
  function episodeKey(clip) { return clip.media_type === 'episode' ? [clip.season_number, clip.episode_number, clip.title].join('|') : ''; }

  function setOptions(select, values, allLabel, labeler = value => value) {
    const previous = select.value;
    select.replaceChildren(new Option(allLabel, ''));
    values.forEach(value => select.add(new Option(labeler(value), value)));
    select.value = values.includes(previous) ? previous : '';
  }

  function upstreamClips(includeTitle = false) {
    return clips.filter(clip =>
      (!filters.library.value || clip.media_library === filters.library.value) &&
      (!filters.type.value || clip.media_type === filters.type.value) &&
      (!includeTitle || !filters.title.value || titleKey(clip) === filters.title.value)
    );
  }

  function refreshFilterOptions() {
    setOptions(filters.library, unique(clips.map(clip => clip.media_library)), 'All libraries');
    setOptions(filters.title, unique(upstreamClips().map(titleKey)), 'All titles');
    const episodeValues = unique(upstreamClips(true).filter(clip => clip.media_type === 'episode').map(episodeKey));
    const episodeMap = new Map(upstreamClips(true).map(clip => [episodeKey(clip), `${clip.episode_code || 'Episode'} · ${clip.title || 'Untitled episode'}`]));
    setOptions(filters.episode, episodeValues, 'All episodes', value => episodeMap.get(value) || value);
    byId('episode_filter_group').classList.toggle('filter-disabled', !episodeValues.length);
    filters.episode.disabled = !episodeValues.length;
  }

  function filteredClips() {
    const query = filters.search.value.trim().toLocaleLowerCase();
    return clips.filter(clip => {
      const searchable = [clip.clip_title, clip.media_library, clip.display_heading, clip.display_subtitle, clip.title, clip.show, clip.episode_code, clip.year].join(' ').toLocaleLowerCase();
      return (!query || searchable.includes(query)) &&
        (!filters.library.value || clip.media_library === filters.library.value) &&
        (!filters.type.value || clip.media_type === filters.type.value) &&
        (!filters.title.value || titleKey(clip) === filters.title.value) &&
        (!filters.episode.value || episodeKey(clip) === filters.episode.value);
    });
  }

  function icon(name) {
    if (name === 'gif') {
      const mark = document.createElement('span');
      mark.className = 'gif-mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = 'GIF';
      return mark;
    }
    const node = document.createElement('i');
    node.className = `fas fa-${name}`;
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function actionButton(label, iconName, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `clip-action ${className || ''}`.trim();
    button.title = label;
    button.setAttribute('aria-label', label);
    button.append(icon(iconName), Object.assign(document.createElement('span'), {textContent: label}));
    button.addEventListener('click', handler);
    return button;
  }

  function buildClipCard(clip) {
    const card = document.createElement('article');
    card.className = 'library-clip-card';
    card.dataset.path = clip.file_path;

    const preview = document.createElement('button');
    preview.type = 'button';
    preview.className = 'clip-preview';
    preview.setAttribute('aria-label', `Play ${clip.clip_title || clip.display_heading}`);
    const image = document.createElement('img');
    image.src = clip.thumbnail_path;
    image.alt = '';
    image.loading = 'lazy';
    image.addEventListener('error', () => {
      image.remove();
      preview.classList.add('preview-unavailable');
    }, {once: true});
    const play = document.createElement('span');
    play.className = 'clip-play';
    play.appendChild(icon('play'));
    preview.append(image, play);
    preview.addEventListener('click', () => openPlayer(clip));

    const content = document.createElement('div');
    content.className = 'clip-card-content';
    const header = document.createElement('div');
    header.className = 'clip-card-header';
    const heading = document.createElement('h3');
    heading.textContent = clip.clip_title || clip.display_heading;
    const subtitle = document.createElement('p');
    subtitle.textContent = clip.media_type === 'episode'
      ? `${clip.display_heading} · ${clip.display_subtitle}`
      : clip.display_heading;
    header.append(heading, subtitle);

    const meta = document.createElement('div');
    meta.className = 'clip-meta';
    const start = document.createElement('span');
    start.append(icon('cut'), document.createTextNode(` ${clip.original_start_time || '00:00:00.000'}–${clip.original_end_time || clip.original_start_time || '00:00:00.000'}`));
    const creator = document.createElement('span');
    creator.append(icon('user'), document.createTextNode(` ${clip.username || 'Unknown creator'}`));
    meta.append(start, creator);

    const actions = document.createElement('div');
    actions.className = 'clip-actions';
    actions.append(
      actionButton('Play', 'play', 'primary-action', () => openPlayer(clip)),
      actionButton('Trim clip', 'cut', '', () => window.ClipTrimmer.open(clip, 'clip', clips)),
      actionButton('Extend from original', 'expand-alt', '', () => window.ClipTrimmer.open(clip, 'original', clips)),
      actionButton('Edit details', 'pen', '', () => openEdit(clip)),
      actionButton('Export GIF', 'gif', '', () => exportGif(clip)),
    );
    const download = document.createElement('a');
    download.className = 'clip-action';
    download.href = clip.file_path;
    download.download = '';
    download.title = 'Download';
    download.setAttribute('aria-label', 'Download clip');
    download.append(icon('download'), Object.assign(document.createElement('span'), {textContent: 'Download'}));
    actions.appendChild(download);
    if (state.uploaders.length) actions.appendChild(actionButton('Upload', 'share-square', '', () => openUpload(clip)));
    actions.appendChild(actionButton('Delete', 'trash-alt', 'danger-action', () => openDelete(clip)));
    content.append(header, meta, actions);
    card.append(preview, content);
    return card;
  }

  function render() {
    refreshFilterOptions();
    const visible = filteredClips();
    libraryRoot.replaceChildren();
    const grouped = new Map();
    visible.forEach(clip => {
      const library = clip.media_library || 'Uncategorized';
      if (!grouped.has(library)) grouped.set(library, []);
      grouped.get(library).push(clip);
    });
    Array.from(grouped.keys()).sort((a, b) => a.localeCompare(b)).forEach((library, index) => {
      const section = document.createElement('section');
      section.className = 'media-library-group';
      const groupHeader = document.createElement('div');
      groupHeader.className = 'library-group-header';
      const title = document.createElement('h2');
      const toggle = document.createElement('button');
      const gridId = `library-group-${index}`;
      const collapsed = state.collapsedLibraries.has(library);
      toggle.type = 'button';
      toggle.className = 'library-group-toggle';
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-controls', gridId);
      const toggleIcon = icon(collapsed ? 'chevron-right' : 'chevron-down');
      toggle.append(toggleIcon, document.createTextNode(library));
      title.appendChild(toggle);
      const count = document.createElement('span');
      const amount = grouped.get(library).length;
      count.textContent = `${amount} ${amount === 1 ? 'clip' : 'clips'}`;
      groupHeader.append(title, count);
      const grid = document.createElement('div');
      grid.className = 'clip-card-grid';
      grid.id = gridId;
      grid.hidden = collapsed;
      grouped.get(library).forEach(clip => grid.appendChild(buildClipCard(clip)));
      toggle.addEventListener('click', () => {
        const shouldCollapse = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', String(!shouldCollapse));
        toggleIcon.className = `fas fa-chevron-${shouldCollapse ? 'right' : 'down'}`;
        grid.hidden = shouldCollapse;
        if (shouldCollapse) state.collapsedLibraries.add(library);
        else state.collapsedLibraries.delete(library);
      });
      section.append(groupHeader, grid);
      libraryRoot.appendChild(section);
    });
    byId('clip_count').textContent = visible.length === clips.length ? `${clips.length} ${clips.length === 1 ? 'clip' : 'clips'}` : `${visible.length} of ${clips.length} clips`;
    const activeFilterCount = [filters.search, filters.library, filters.type, filters.title, filters.episode].filter(control => control.value).length;
    byId('filter_count').textContent = String(activeFilterCount);
    byId('filter_count').classList.toggle('d-none', activeFilterCount === 0);
    byId('library_empty').classList.toggle('d-none', visible.length !== 0);
    libraryRoot.classList.toggle('d-none', visible.length === 0);
    libraryRoot.dataset.view = state.view;
    libraryRoot.dataset.size = state.size;
    byId('grid_size_group').classList.toggle('d-none', state.view !== 'grid');
    byId('grid_view').classList.toggle('active', state.view === 'grid');
    byId('list_view').classList.toggle('active', state.view === 'list');
    byId('grid_view').setAttribute('aria-pressed', String(state.view === 'grid'));
    byId('list_view').setAttribute('aria-pressed', String(state.view === 'list'));
  }

  function notice(message, tone = 'success') {
    const alert = document.createElement('div');
    alert.className = `alert alert-${tone}`;
    alert.textContent = message;
    byId('library_notice').replaceChildren(alert);
    window.setTimeout(() => { if (alert.isConnected) alert.remove(); }, 5000);
  }

  async function refreshClips() {
    const request = ++refreshRequest;
    const sort = state.sort;
    const response = await fetch(`/api/clips?sort=${encodeURIComponent(sort)}`, {cache: 'no-store'});
    const result = await response.json();
    if (request !== refreshRequest) return;
    if (!response.ok) throw new Error(result.message || 'The clip library could not be refreshed.');
    clips = result.clips || [];
    render();
  }

  function openPlayer(clip) {
    const player = byId('library_player');
    byId('player_modal_title').textContent = clip.clip_title || clip.display_heading;
    byId('player_modal_subtitle').textContent = clip.media_type === 'episode'
      ? `${clip.display_heading} · ${clip.display_subtitle}`
      : clip.display_heading;
    player.poster = clip.thumbnail_path;
    player.src = clip.file_path;
    bootstrap.Modal.getOrCreateInstance(byId('player_modal')).show();
  }

  function updateEditFields() {
    const episodic = byId('edit_media_type').value === 'episode';
    byId('edit_episode_fields').classList.toggle('d-none', !episodic);
    byId('edit_year_group').classList.toggle('d-none', episodic);
    byId('edit_title_label').textContent = episodic ? 'Episode title' : 'Movie name';
  }

  function openEdit(clip) {
    state.editingPath = clip.file_path;
    byId('edit_clip_title').textContent = `Edit ${clip.clip_title || clip.display_heading}`;
    byId('edit_custom_title').value = clip.clip_title || '';
    byId('edit_media_library').value = clip.media_library === 'Uncategorized' ? '' : clip.media_library;
    byId('edit_media_type').value = clip.media_type;
    byId('edit_title').value = clip.title || '';
    byId('edit_year').value = clip.year || '';
    byId('edit_show').value = clip.show || '';
    byId('edit_season').value = clip.season_number || '';
    byId('edit_episode').value = clip.episode_number || '';
    byId('edit_clip_error').classList.add('d-none');
    updateEditFields();
    bootstrap.Modal.getOrCreateInstance(byId('edit_clip_modal')).show();
  }

  async function saveEdit(event) {
    event.preventDefault();
    if (!state.editingPath) return;
    const button = byId('save_clip_details');
    button.disabled = true;
    const payload = {
      file_path: state.editingPath,
      clip_title: byId('edit_custom_title').value,
      media_library: byId('edit_media_library').value,
      media_type: byId('edit_media_type').value,
      title: byId('edit_title').value,
      year: byId('edit_year').value,
      show: byId('edit_show').value,
      season_number: byId('edit_season').value,
      episode_number: byId('edit_episode').value,
    };
    try {
      const response = await fetch('/api/clips/metadata', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Clip details could not be saved.');
      bootstrap.Modal.getInstance(byId('edit_clip_modal')).hide();
      notice('Clip details saved.');
      try { await refreshClips(); }
      catch (error) { notice(`Clip details were saved, but the library could not refresh: ${error.message}`, 'warning'); }
    } catch (error) {
      byId('edit_clip_error').textContent = error.message;
      byId('edit_clip_error').classList.remove('d-none');
    } finally {
      button.disabled = false;
    }
  }

  function openDelete(clip) {
    state.deletingPath = clip.file_path;
    window.clearTimeout(state.deleteTimer);
    byId('delete_clip_message').textContent = `Delete “${clip.clip_title || clip.display_heading}”?`;
    byId('delete_clip_error').classList.add('d-none');
    const confirm = byId('confirm_delete_clip');
    confirm.disabled = true;
    confirm.textContent = 'Delete in 1…';
    bootstrap.Modal.getOrCreateInstance(byId('delete_clip_modal')).show();
    state.deleteTimer = window.setTimeout(() => {
      if (state.deletingPath === clip.file_path) {
        confirm.disabled = false;
        confirm.textContent = 'Delete clip';
      }
    }, 750);
  }

  async function confirmDelete() {
    if (!state.deletingPath) return;
    const path = state.deletingPath;
    const button = byId('confirm_delete_clip');
    button.disabled = true;
    button.textContent = 'Deleting…';
    try {
      const response = await fetch('/api/clips', {method: 'DELETE', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file_path: path})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'The clip could not be deleted.');
      state.deletingPath = null;
      bootstrap.Modal.getInstance(byId('delete_clip_modal')).hide();
      notice('Clip deleted.');
      try { await refreshClips(); }
      catch (error) { notice(`The clip was deleted, but the library could not refresh: ${error.message}`, 'warning'); }
    } catch (error) {
      byId('delete_clip_error').textContent = error.message;
      byId('delete_clip_error').classList.remove('d-none');
      button.disabled = false;
      button.textContent = 'Try again';
    }
  }

  function selectedValues(id) { return Array.from(byId(id).selectedOptions).map(option => option.value); }

  async function loadImmichOptions() {
    if (state.immichLoaded) return;
    byId('library_immich_loading').classList.remove('d-none');
    byId('library_immich_fields').classList.add('d-none');
    try {
      const response = await fetch('/api/uploaders/immich/options', {cache: 'no-store'});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Immich options could not be loaded.');
      const fill = (id, options) => {
        const select = byId(id); select.replaceChildren();
        options.forEach(item => select.add(new Option(item.name, item.id)));
      };
      fill('library_immich_tags', result.tags || []);
      fill('library_immich_albums', result.albums || []);
      state.immichLoaded = true;
      byId('library_immich_fields').classList.remove('d-none');
    } finally {
      byId('library_immich_loading').classList.add('d-none');
    }
  }

  async function updateUploadOptions() {
    const immich = byId('library_upload_service').value === 'immich';
    byId('library_immich_options').classList.toggle('d-none', !immich);
    byId('library_upload_status').replaceChildren();
    if (immich) {
      try { await loadImmichOptions(); }
      catch (error) { byId('library_upload_status').innerHTML = `<div class="alert alert-danger"></div>`; byId('library_upload_status').querySelector('.alert').textContent = error.message; }
    }
  }

  function openUpload(clip) {
    state.uploadingPath = clip.file_path;
    byId('upload_clip_title').textContent = `Upload ${clip.clip_title || clip.display_heading}`;
    const select = byId('library_upload_service');
    select.replaceChildren();
    state.uploaders.forEach(uploader => select.add(new Option(uploader.label, uploader.id)));
    byId('library_new_tags').value = '';
    byId('library_new_album').value = '';
    updateUploadOptions();
    bootstrap.Modal.getOrCreateInstance(byId('upload_clip_modal')).show();
  }

  async function submitUpload() {
    if (!state.uploadingPath) return;
    const button = byId('library_upload_submit');
    button.disabled = true;
    const service = byId('library_upload_service').value;
    const payload = {file_path: state.uploadingPath, uploader: service};
    if (service === 'immich') {
      payload.tag_ids = selectedValues('library_immich_tags');
      payload.album_ids = selectedValues('library_immich_albums');
      payload.tag_names = byId('library_new_tags').value.split(',').map(value => value.trim()).filter(Boolean);
      payload.new_album_name = byId('library_new_album').value.trim();
    }
    try {
      const response = await fetch('/api/uploads', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok && response.status !== 207) throw new Error(result.message || 'Upload failed.');
      bootstrap.Modal.getInstance(byId('upload_clip_modal')).hide();
      notice(response.status === 207 ? 'Clip uploaded, but some organization steps failed.' : 'Clip uploaded successfully.', response.status === 207 ? 'warning' : 'success');
    } catch (error) {
      byId('library_upload_status').innerHTML = '<div class="alert alert-danger"></div>';
      byId('library_upload_status').querySelector('.alert').textContent = error.message;
    } finally { button.disabled = false; }
  }

  function downloadGif(exported) {
    const link = document.createElement('a');
    link.href = exported.download_url;
    link.download = exported.filename || '';
    document.body.appendChild(link);
    link.click();
    link.remove();
    notice(exported.cached ? 'Cached GIF downloaded.' : 'GIF exported and downloaded.');
  }

  function showJob(job) {
    const percent = Math.max(0, Math.min(100, Number(job.overall_progress) || 0));
    byId('library_job_percent').textContent = `${Math.round(percent)}%`;
    byId('library_job_bar').style.width = `${percent}%`;
    byId('library_job_bar').setAttribute('aria-valuenow', String(percent));
    byId('library_job_message').textContent = job.message || job.stage || 'Working…';
  }

  async function pollGif(jobId) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
      const job = await response.json();
      if (!response.ok) throw new Error(job.message || 'GIF progress is unavailable.');
      showJob(job);
      if (job.status === 'queued' || job.status === 'running') {
        window.setTimeout(() => pollGif(jobId), 600);
      } else if (job.status === 'succeeded' && job.result && job.result.export) {
        bootstrap.Modal.getInstance(byId('library_job_modal')).hide();
        downloadGif(job.result.export);
      } else {
        throw new Error((job.error && job.error.message) || job.message || 'GIF export failed.');
      }
    } catch (error) {
      bootstrap.Modal.getInstance(byId('library_job_modal')).hide();
      notice(error.message, 'danger');
    }
  }

  async function exportGif(clip) {
    try {
      const response = await fetch('/api/gif-exports', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file_path: clip.file_path})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'GIF export could not start.');
      if (result.export) return downloadGif(result.export);
      showJob({overall_progress: 0, message: 'Queued'});
      bootstrap.Modal.getOrCreateInstance(byId('library_job_modal')).show();
      pollGif(result.job_id);
    } catch (error) { notice(error.message, 'danger'); }
  }

  function resetFilters() {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = null;
    Object.values(filters).forEach(control => { control.value = ''; });
    render();
    filters.search.focus();
  }

  async function loadUploaders() {
    try {
      const response = await fetch('/api/uploaders', {cache: 'no-store'});
      if (response.ok) state.uploaders = (await response.json()).uploaders || [];
    } finally { render(); }
  }

  filters.search.addEventListener('input', () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      state.searchTimer = null;
      render();
    }, 150);
  });
  [filters.library, filters.type, filters.title, filters.episode].forEach(control => control.addEventListener('change', render));
  byId('reset_filters').addEventListener('click', resetFilters);
  document.querySelectorAll('[data-reset-filters]').forEach(button => button.addEventListener('click', resetFilters));
  byId('grid_view').addEventListener('click', () => { state.view = 'grid'; localStorage.setItem('clipplex.libraryView', state.view); render(); });
  byId('list_view').addEventListener('click', () => { state.view = 'list'; localStorage.setItem('clipplex.libraryView', state.view); render(); });
  byId('grid_size').value = state.size;
  byId('grid_size').addEventListener('change', event => { state.size = event.target.value; localStorage.setItem('clipplex.librarySize', state.size); render(); });
  byId('sort_order').value = state.sort;
  byId('sort_order').addEventListener('change', async event => {
    state.sort = event.target.value;
    localStorage.setItem('clipplex.librarySort', state.sort);
    try { await refreshClips(); }
    catch (error) { notice(error.message, 'danger'); }
  });
  byId('edit_media_type').addEventListener('change', updateEditFields);
  byId('edit_clip_form').addEventListener('submit', saveEdit);
  byId('confirm_delete_clip').addEventListener('click', confirmDelete);
  byId('delete_clip_modal').addEventListener('shown.bs.modal', () => byId('cancel_delete_clip').focus());
  byId('delete_clip_modal').addEventListener('hidden.bs.modal', () => { window.clearTimeout(state.deleteTimer); state.deletingPath = null; });
  byId('player_modal').addEventListener('hidden.bs.modal', () => { const player = byId('library_player'); player.pause(); player.removeAttribute('src'); player.removeAttribute('poster'); player.load(); });
  byId('library_upload_service').addEventListener('change', updateUploadOptions);
  byId('library_upload_submit').addEventListener('click', submitUpload);
  document.addEventListener('clipplex:clip-saved', async event => {
    const result = event.detail || {};
    if (!result.clip) return;
    notice(result.operation === 'replace' ? 'Clip replaced.' : 'Trimmed copy saved.');
    try { await refreshClips(); }
    catch (error) { notice(`The clip was saved, but the library could not refresh: ${error.message}`, 'warning'); }
  });

  render();
  if (state.sort !== (dataNode?.dataset.sort || 'newest')) refreshClips().catch(error => notice(error.message, 'danger'));
  loadUploaders();
})();
