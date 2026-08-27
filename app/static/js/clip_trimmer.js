(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const state = {
    clip: null, basis: 'clip', windowStart: 0, windowEnd: 0,
    selectionStart: 0, selectionEnd: 0, sourceDuration: 0,
    previewFrame: null, titleSuggested: '', titleDirty: false,
    busy: false, hasEditor: false,
  };

  function formatTime(milliseconds) {
    let value = Math.max(0, Math.round(Number(milliseconds) || 0));
    const hours = Math.floor(value / 3600000); value %= 3600000;
    const minutes = Math.floor(value / 60000); value %= 60000;
    const seconds = Math.floor(value / 1000); const millis = value % 1000;
    return [hours, minutes, seconds].map(number => String(number).padStart(2, '0')).join(':') + '.' + String(millis).padStart(3, '0');
  }

  function parseTime(value) {
    const match = String(value || '').trim().match(/^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/);
    if (!match) return null;
    const fraction = (match[4] || '0').padEnd(3, '0');
    return ((Number(match[1]) * 60 + Number(match[2])) * 60 + Number(match[3])) * 1000 + Number(fraction);
  }

  function baseTitle(clip) {
    if (clip.media_type === 'episode') {
      return [clip.show || 'Unknown Series', clip.episode_code, clip.title || 'Untitled episode'].filter(Boolean).join(' - ');
    }
    return clip.display_heading || clip.title || 'Untitled movie';
  }

  function suggestedTitle(clip, loadedClips = null, nextClipNumber = null) {
    const suppliedNumber = Number(nextClipNumber);
    const sameSource = Array.isArray(loadedClips)
      ? loadedClips.filter(item => item.source_key === clip.source_key)
      : [clip];
    const inferredNumber = Math.max(Number(clip.clip_number) || 0, ...sameSource.map(item => Number(item.clip_number) || 0)) + 1;
    const number = suppliedNumber > 0 ? suppliedNumber : Math.max(1, inferredNumber);
    return baseTitle(clip) + (number > 1 ? ` - ${number}` : '');
  }

  function error(message) {
    const alert = byId('clip_trim_error');
    alert.textContent = message;
    alert.classList.remove('d-none');
  }

  function clearError() { byId('clip_trim_error').classList.add('d-none'); }

  function showOnly(section) {
    ['clip_source_setup', 'clip_trim_progress', 'clip_trim_editor'].forEach(id => byId(id).classList.toggle('d-none', id !== section));
    byId('clip_trim_footer').classList.toggle('d-none', section !== 'clip_trim_editor');
  }

  function setBusy(busy) {
    state.busy = busy;
    byId('save_trimmed_copy').disabled = busy;
    byId('replace_trimmed_clip').disabled = busy;
    byId('load_source_preview').disabled = busy || !byId('clip_source_audio').value;
  }

  function updateRange() {
    const duration = Math.max(1, state.windowEnd - state.windowStart);
    const startLocal = Math.max(0, state.selectionStart - state.windowStart);
    const endLocal = Math.min(duration, state.selectionEnd - state.windowStart);
    const startRange = byId('clip_trim_start_range');
    const endRange = byId('clip_trim_end_range');
    [startRange, endRange].forEach(input => { input.max = String(duration); });
    startRange.value = String(startLocal);
    endRange.value = String(endLocal);
    const timeline = byId('clip_trim_timeline');
    timeline.style.setProperty('--trim-start', `${startLocal / duration * 100}%`);
    timeline.style.setProperty('--trim-end', `${endLocal / duration * 100}%`);
    const shownStart = state.basis === 'original' ? state.selectionStart : startLocal;
    const shownEnd = state.basis === 'original' ? state.selectionEnd : endLocal;
    byId('clip_trim_start_text').value = formatTime(shownStart);
    byId('clip_trim_end_text').value = formatTime(shownEnd);
    byId('clip_trim_duration').textContent = formatTime(state.selectionEnd - state.selectionStart);
    const originalBase = parseTime(state.clip.original_start_time) || 0;
    const sourceStart = state.basis === 'original' ? state.selectionStart : originalBase + startLocal;
    const sourceEnd = state.basis === 'original' ? state.selectionEnd : originalBase + endLocal;
    byId('clip_trim_source_range').textContent = `Source ${formatTime(sourceStart)}–${formatTime(sourceEnd)}`;
    byId('load_source_earlier').disabled = state.windowStart <= 0;
    byId('load_source_later').disabled = state.windowEnd >= state.sourceDuration;
  }

  function setSelection(start, end, seekBoundary) {
    const minimum = 100;
    state.selectionStart = Math.max(state.windowStart, Math.min(Number(start), state.windowEnd - minimum));
    state.selectionEnd = Math.min(state.windowEnd, Math.max(Number(end), state.selectionStart + minimum));
    updateRange();
    if (seekBoundary) {
      const target = seekBoundary === 'start' ? state.selectionStart : state.selectionEnd;
      byId('clip_trim_player').currentTime = Math.max(0, (target - state.windowStart) / 1000);
    }
  }

  function applyTimeInput(event, boundary) {
    const value = parseTime(event.target.value);
    if (value === null) {
      updateRange();
      return;
    }
    if (boundary === 'start') {
      setSelection(state.basis === 'original' ? value : state.windowStart + value, state.selectionEnd, 'start');
    } else {
      setSelection(state.selectionStart, state.basis === 'original' ? value : state.windowStart + value, 'end');
    }
  }

  function stopPreview() {
    if (state.previewFrame) cancelAnimationFrame(state.previewFrame);
    state.previewFrame = null;
    const button = byId('preview_trim_selection');
    button.innerHTML = '<i class="fas fa-play me-2" aria-hidden="true"></i>Preview selection';
  }

  function previewTick() {
    const player = byId('clip_trim_player');
    const endLocal = (state.selectionEnd - state.windowStart) / 1000;
    if (player.paused || player.currentTime >= endLocal - 0.015) {
      player.pause();
      if (player.currentTime >= endLocal - 0.015) player.currentTime = endLocal;
      stopPreview();
      return;
    }
    state.previewFrame = requestAnimationFrame(previewTick);
  }

  async function previewSelection() {
    const player = byId('clip_trim_player');
    stopPreview();
    player.currentTime = Math.max(0, (state.selectionStart - state.windowStart) / 1000);
    try {
      await player.play();
      byId('preview_trim_selection').innerHTML = '<i class="fas fa-stop me-2" aria-hidden="true"></i>Stop preview';
      state.previewFrame = requestAnimationFrame(previewTick);
    } catch (playError) { error('The browser could not start the preview.'); }
  }

  function fillTracks(tracks) {
    const fill = (id, options) => {
      const select = byId(id); select.replaceChildren();
      (options || []).forEach(item => {
        const option = new Option(item.label, item.id); option.disabled = item.available === false; option.selected = Boolean(item.selected); select.add(option);
      });
      if (!select.value) {
        const available = Array.from(select.options).find(option => !option.disabled);
        if (available) select.value = available.value;
      }
    };
    fill('clip_source_audio', tracks.audio);
    fill('clip_source_subtitle', tracks.subtitles);
    byId('clip_source_tracks').classList.remove('d-none');
    byId('load_source_preview').disabled = !byId('clip_source_audio').value;
  }

  async function loadSourceOptions() {
    showOnly('clip_source_setup'); setBusy(true); clearError();
    byId('clip_source_message').textContent = 'Checking the saved original Plex media…';
    byId('clip_source_tracks').classList.add('d-none');
    byId('clip_source_audio').replaceChildren();
    byId('clip_source_subtitle').replaceChildren();
    try {
      const params = new URLSearchParams({file_path: state.clip.file_path});
      const response = await fetch(`/api/clips/source-options?${params}`, {cache: 'no-store'});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Original-source options could not be loaded.');
      if (result.status !== 'ready') throw new Error(result.message || 'The saved original source is unavailable.');
      state.sourceDuration = Number(result.source_duration_ms) || 0;
      fillTracks(result.tracks || {});
      byId('clip_source_message').textContent = 'The saved original source is available.';
    } catch (sourceError) {
      byId('clip_source_message').textContent = 'This clip cannot be extended from its original source.';
      error(sourceError.message);
    }
    finally { setBusy(false); }
  }

  function showEditor(url, windowStart, windowEnd, selectionStart, selectionEnd) {
    state.hasEditor = true;
    state.windowStart = Number(windowStart);
    state.windowEnd = Number(windowEnd);
    state.selectionStart = Number(selectionStart);
    state.selectionEnd = Number(selectionEnd);
    const player = byId('clip_trim_player');
    player.src = url;
    player.load();
    byId('clip_trim_start_label').textContent = state.basis === 'original' ? 'Start in source' : 'Start in clip';
    byId('clip_trim_end_label').textContent = state.basis === 'original' ? 'End in source' : 'End in clip';
    byId('load_source_earlier').classList.toggle('d-none', state.basis !== 'original');
    byId('load_source_later').classList.toggle('d-none', state.basis !== 'original');
    showOnly('clip_trim_editor'); updateRange(); setBusy(false);
  }

  function showProgress(message) {
    showOnly('clip_trim_progress');
    byId('clip_trim_progress_message').textContent = message || 'Queued';
    byId('clip_trim_progress_percent').textContent = '0%';
    byId('clip_trim_progress_bar').style.width = '0%';
  }

  async function pollJob(jobId, onSuccess) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
      const job = await response.json();
      if (!response.ok) throw new Error(job.message || 'Job progress is unavailable.');
      const percent = Math.max(0, Math.min(100, Number(job.overall_progress) || 0));
      byId('clip_trim_progress_message').textContent = job.message || job.stage || 'Working…';
      byId('clip_trim_progress_percent').textContent = `${Math.round(percent)}%`;
      byId('clip_trim_progress_bar').style.width = `${percent}%`;
      byId('clip_trim_progress_bar').setAttribute('aria-valuenow', String(percent));
      if (job.status === 'queued' || job.status === 'running') return window.setTimeout(() => pollJob(jobId, onSuccess), 600);
      if (job.status === 'succeeded') return onSuccess(job.result);
      throw new Error((job.error && job.error.message) || job.message || 'The media job failed.');
    } catch (jobError) {
      showOnly(state.basis === 'original' && !state.hasEditor ? 'clip_source_setup' : 'clip_trim_editor');
      setBusy(false); error(jobError.message);
    }
  }

  async function requestSourcePreview(windowStart, windowEnd) {
    setBusy(true); clearError(); stopPreview(); showProgress('Preparing original-source preview…');
    const payload = {
      file_path: state.clip.file_path, expected_revision: state.clip.revision,
      audio_stream_id: byId('clip_source_audio').value,
      subtitle_stream_id: byId('clip_source_subtitle').value || 'none',
      window_start_ms: windowStart, window_end_ms: windowEnd,
      selection_start_ms: state.hasEditor ? state.selectionStart : undefined,
      selection_end_ms: state.hasEditor ? state.selectionEnd : undefined,
    };
    try {
      const response = await fetch('/api/clip-extension-previews', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'The original preview could not start.');
      pollJob(result.job_id, jobResult => {
        const preview = jobResult.preview;
        state.sourceDuration = Number(preview.source_duration_ms) || state.sourceDuration;
        showEditor(preview.url, preview.window_start_ms, preview.window_end_ms, preview.selection_start_ms, preview.selection_end_ms);
      });
    } catch (sourceError) { showOnly('clip_source_setup'); setBusy(false); error(sourceError.message); }
  }

  async function save(mode) {
    if (state.selectionEnd - state.selectionStart < 100) return error('Select at least 100 milliseconds.');
    setBusy(true); clearError(); stopPreview(); byId('clip_trim_player').pause();
    const customTitle = state.titleDirty && byId('trim_new_title').value.trim() !== state.titleSuggested
      ? byId('trim_new_title').value.trim() : '';
    const payload = {
      file_path: state.clip.file_path, expected_revision: state.clip.revision,
      start_ms: state.selectionStart, end_ms: state.selectionEnd,
      basis: state.basis, mode, custom_title: customTitle,
    };
    showProgress(mode === 'replace' ? 'Replacing clip…' : 'Saving trimmed copy…');
    try {
      const response = await fetch('/api/clip-trims', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'The trim job could not start.');
      pollJob(result.job_id, jobResult => {
        setBusy(false);
        bootstrap.Modal.getOrCreateInstance(byId('clip_trim_modal')).hide();
        document.dispatchEvent(new CustomEvent('clipplex:clip-saved', {detail: jobResult}));
      });
    } catch (saveError) { showOnly('clip_trim_editor'); setBusy(false); error(saveError.message); }
  }

  function open(clip, basis = 'clip', loadedClips = null, nextClipNumber = null) {
    state.clip = clip; state.basis = basis; state.titleDirty = false; state.hasEditor = false; clearError(); stopPreview();
    byId('clip_trim_title').textContent = basis === 'original' ? 'Extend from original' : `Trim ${clip.clip_title || clip.display_heading || 'clip'}`;
    byId('clip_trim_subtitle').textContent = basis === 'original' ? (clip.clip_title || '') : 'Move either boundary inward, then preview before saving.';
    state.titleSuggested = suggestedTitle(clip, loadedClips, nextClipNumber);
    byId('trim_new_title').value = state.titleSuggested;
    byId('trim_new_title').disabled = false;
    bootstrap.Modal.getOrCreateInstance(byId('clip_trim_modal')).show();
    if (basis === 'clip') {
      state.sourceDuration = Number(clip.duration_ms) || 0;
      showEditor(clip.file_path, 0, state.sourceDuration, 0, state.sourceDuration);
    } else {
      loadSourceOptions();
    }
  }

  byId('clip_trim_start_range').addEventListener('input', event => setSelection(state.windowStart + Number(event.target.value), state.selectionEnd, 'start'));
  byId('clip_trim_end_range').addEventListener('input', event => setSelection(state.selectionStart, state.windowStart + Number(event.target.value), 'end'));
  byId('clip_trim_start_text').addEventListener('change', event => applyTimeInput(event, 'start'));
  byId('clip_trim_end_text').addEventListener('change', event => applyTimeInput(event, 'end'));
  byId('set_trim_start').addEventListener('click', () => setSelection(state.windowStart + byId('clip_trim_player').currentTime * 1000, state.selectionEnd, 'start'));
  byId('set_trim_end').addEventListener('click', () => setSelection(state.selectionStart, state.windowStart + byId('clip_trim_player').currentTime * 1000, 'end'));
  byId('preview_trim_selection').addEventListener('click', () => state.previewFrame ? (byId('clip_trim_player').pause(), stopPreview()) : previewSelection());
  byId('trim_new_title').addEventListener('input', () => { state.titleDirty = true; });
  byId('clip_source_audio').addEventListener('change', () => { byId('load_source_preview').disabled = !byId('clip_source_audio').value; });
  byId('load_source_preview').addEventListener('click', () => requestSourcePreview(null, null));
  byId('load_source_earlier').addEventListener('click', () => requestSourcePreview(Math.max(0, state.windowStart - 30000), state.windowEnd));
  byId('load_source_later').addEventListener('click', () => requestSourcePreview(state.windowStart, Math.min(state.sourceDuration, state.windowEnd + 30000)));
  byId('save_trimmed_copy').addEventListener('click', () => save('new'));
  byId('replace_trimmed_clip').addEventListener('click', () => {
    byId('replace_trim_message').textContent = `Replace “${state.clip.clip_title || state.clip.display_heading || 'this clip'}” with the ${formatTime(state.selectionEnd - state.selectionStart)} selection?`;
    byId('replace_trim_error').classList.add('d-none');
    bootstrap.Modal.getOrCreateInstance(byId('replace_trim_modal')).show();
  });
  byId('confirm_replace_trim').addEventListener('click', () => {
    bootstrap.Modal.getOrCreateInstance(byId('replace_trim_modal')).hide(); save('replace');
  });
  byId('replace_trim_modal').addEventListener('shown.bs.modal', () => byId('cancel_replace_trim').focus());
  byId('clip_trim_modal').addEventListener('hidden.bs.modal', () => {
    stopPreview(); const player = byId('clip_trim_player'); player.pause(); player.removeAttribute('src'); player.load(); state.clip = null;
  });

  window.ClipTrimmer = {open, formatTime, parseTime};
})();
