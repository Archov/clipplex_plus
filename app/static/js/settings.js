(() => {
  const form = document.getElementById('settings_form');
  const sectionsRoot = document.getElementById('settings_sections');
  const notice = document.getElementById('settings_notice');
  const saveButton = document.getElementById('save_settings');
  let settingsModel = null;
  let activePermissionPopover = null;
  let activePermissionTrigger = null;

  function showNotice(message, style = 'success') {
    notice.replaceChildren();
    if (!message) return;
    const alert = document.createElement('div');
    alert.className = `alert alert-${style}`;
    alert.textContent = message;
    notice.append(alert);
  }

  function resetTestButton(service) {
    const button = document.querySelector(`[data-test-service="${service}"]`);
    if (!button || !button.dataset.testResult) return;
    button.className = 'btn btn-outline-secondary btn-sm';
    button.innerHTML = '<i class="fas fa-plug me-2" aria-hidden="true"></i>Test saved connection';
    delete button.dataset.testResult;
  }

  function showTestResult(button, message, succeeded) {
    button.disabled = false;
    button.dataset.testResult = 'true';
    button.className = `btn btn-outline-${succeeded ? 'success' : 'danger'} btn-sm`;
    button.textContent = message;
  }

  function immichApiSettingsUrl(value) {
    try {
      const url = new URL(value.trim());
      if (!['http:', 'https:'].includes(url.protocol)) return null;
      return `${url.href.replace(/\/+$/, '')}/user-settings?isOpen=api-keys`;
    } catch (_) {
      return null;
    }
  }

  function updateImmichApiKeyLink(value, link = document.querySelector('[data-create-immich-api-key]')) {
    if (!link) return;
    const target = immichApiSettingsUrl(value);
    if (target) {
      link.href = target;
      link.removeAttribute('title');
      link.removeAttribute('aria-disabled');
      link.classList.remove('immich-api-key-link-disabled');
    } else {
      link.removeAttribute('href');
      link.title = 'Enter your Immich URL to navigate to your API settings.';
      link.setAttribute('aria-disabled', 'true');
      link.classList.add('immich-api-key-link-disabled');
    }
  }

  document.addEventListener('click', event => {
    if (!activePermissionPopover || !activePermissionTrigger) return;
    if (activePermissionTrigger.contains(event.target) || event.target.closest('.clip-popover')) return;
    activePermissionPopover.hide();
  });

  function fieldNode(field) {
    const wrapper = document.createElement('div');
    wrapper.className = 'mb-3';
    const id = `setting_${field.key}`;
    const label = document.createElement('label');
    label.className = 'form-label';
    label.htmlFor = id;
    label.textContent = field.label;
    if (field.permissions?.length) {
      const createApiKey = document.createElement('a');
      createApiKey.className = 'small ms-2 text-decoration-none';
      createApiKey.target = '_blank';
      createApiKey.rel = 'noopener noreferrer';
      createApiKey.tabIndex = 0;
      createApiKey.dataset.createImmichApiKey = 'true';
      createApiKey.textContent = 'Create an API Key';
      label.append(' ', createApiKey);
      const immichUrl = settingsModel?.fields.find(item => item.key === 'immich_url')?.value || '';
      updateImmichApiKeyLink(immichUrl, createApiKey);

      const info = document.createElement('button');
      info.type = 'button';
      info.className = 'btn btn-link btn-sm p-0 ms-1 align-baseline text-decoration-none';
      info.textContent = 'Required Permissions';
      const permissions = field.permissions.map(permission => `<li>${permission}</li>`).join('');
      info.setAttribute('aria-label', 'Show required Immich API key permissions');
      info.setAttribute('data-bs-placement', 'right');
      info.setAttribute('data-bs-html', 'true');
      info.setAttribute('data-bs-title', 'Required permissions');
      info.setAttribute('data-bs-content', `<ul class="mb-0 ps-3">${permissions}</ul>`);
      label.append(' ', info);
      if (window.bootstrap?.Popover) {
        const popover = new window.bootstrap.Popover(info, {
          trigger: 'click',
          template: '<div class="popover clip-popover" role="tooltip"><div class="popover-arrow"></div><h3 class="popover-header"></h3><div class="popover-body"></div></div>',
        });
        info.addEventListener('shown.bs.popover', () => {
          activePermissionPopover = popover;
          activePermissionTrigger = info;
        });
        info.addEventListener('hidden.bs.popover', () => {
          if (activePermissionTrigger === info) {
            activePermissionPopover = null;
            activePermissionTrigger = null;
          }
        });
      }
    }
    if (field.help_url) {
      const helpLink = document.createElement('a');
      helpLink.className = 'small ms-2 text-decoration-none';
      helpLink.href = field.help_url;
      helpLink.target = '_blank';
      helpLink.rel = 'noopener noreferrer';
      helpLink.innerHTML = '<i class="fas fa-question-circle me-1" aria-hidden="true"></i>';
      helpLink.append(field.help_link_label || 'Help');
      label.append(' ', helpLink);
    }
    const isCheckbox = field.kind === 'checkbox';
    if (!isCheckbox) wrapper.append(label);

    let control;
    if (field.kind === 'select') {
      control = document.createElement('select');
      for (const optionValue of field.options || []) {
        const option = document.createElement('option');
        option.value = optionValue;
        option.textContent = optionValue;
        option.selected = optionValue === field.value;
        control.append(option);
      }
    } else {
      control = document.createElement('input');
      control.type = field.kind || 'text';
      if (field.kind === 'checkbox') control.checked = field.value === 'true';
      else if (!field.secret) control.value = field.value || '';
      if (field.secret) {
        control.autocomplete = 'new-password';
        control.placeholder = field.configured ? 'Saved — leave blank to keep it' : 'Not configured';
      }
    }
    control.className = isCheckbox ? 'form-check-input' : (field.kind === 'select' ? 'form-select' : 'form-control');
    control.id = id;
    control.name = field.key;
    control.dataset.settingKey = field.key;
    control.dataset.secret = String(Boolean(field.secret));
    control.disabled = field.environment_managed;
    control.addEventListener('input', () => resetTestButton(field.section));
    control.addEventListener('change', () => resetTestButton(field.section));
    if (field.key === 'immich_url') {
      control.addEventListener('input', () => updateImmichApiKeyLink(control.value));
      control.addEventListener('change', () => updateImmichApiKeyLink(control.value));
    }
    if (isCheckbox) {
      control.setAttribute('role', 'switch');
      const check = document.createElement('div');
      check.className = 'form-check';
      label.className = 'form-check-label';
      check.append(control, label);
      wrapper.append(check);
    } else {
      wrapper.append(control);
    }

    const help = document.createElement('div');
    help.className = 'form-text';
    help.textContent = field.help;
    wrapper.append(help);
    if (field.environment_managed) {
      const managed = document.createElement('div');
      managed.className = 'form-text text-warning';
      managed.textContent = `Managed by ${field.environment}. Remove that environment variable to edit this value here.`;
      wrapper.append(managed);
    } else if (field.secret && field.configured) {
      const clearWrap = document.createElement('div');
      clearWrap.className = 'form-check mt-2';
      const clear = document.createElement('input');
      clear.className = 'form-check-input';
      clear.type = 'checkbox';
      clear.id = `clear_${field.key}`;
      clear.dataset.clearKey = field.key;
      clear.addEventListener('change', () => resetTestButton(field.section));
      const clearLabel = document.createElement('label');
      clearLabel.className = 'form-check-label';
      clearLabel.htmlFor = clear.id;
      clearLabel.textContent = `Clear saved ${field.label.toLowerCase()}`;
      clearWrap.append(clear, clearLabel);
      wrapper.append(clearWrap);
    }
    return wrapper;
  }

  function sectionNode(section, fields) {
    const column = document.createElement('div');
    column.className = 'col-12 col-xl-6';
    const card = document.createElement('section');
    card.className = 'card h-100';
    const body = document.createElement('div');
    body.className = 'card-body';
    const heading = document.createElement('h2');
    heading.className = 'h4 card-title';
    heading.textContent = section.label;
    body.append(heading);
    let activeGroup = null;
    for (const field of fields) {
      if (field.group && field.group !== activeGroup) {
        const groupHeading = document.createElement('h3');
        groupHeading.className = 'h6 mt-4 mb-3';
        groupHeading.textContent = field.group;
        body.append(groupHeading);
        activeGroup = field.group;
      }
      body.append(fieldNode(field));
    }
    if (['plex', 'streamable', 'immich'].includes(section.id)) {
      const testButton = document.createElement('button');
      testButton.className = 'btn btn-outline-secondary btn-sm';
      testButton.type = 'button';
      testButton.dataset.testService = section.id;
      testButton.innerHTML = '<i class="fas fa-plug me-2" aria-hidden="true"></i>Test saved connection';
      testButton.addEventListener('click', () => testService(section.id, testButton));
      body.append(testButton);
    }
    if (section.id === 'immich') {
      const configured = settingsModel?.fields.find(field => field.key === 'immich_url')?.value &&
        settingsModel?.fields.find(field => field.key === 'immich_api_key')?.configured;
      if (configured) {
        const bulk = document.createElement('button');
        bulk.className = 'btn btn-outline-primary btn-sm ms-2'; bulk.type = 'button';
        bulk.textContent = 'Upload all non-uploaded clips';
        bulk.addEventListener('click', () => queueMissingImmichUploads(bulk));
        body.append(bulk);
      }
    }
    card.append(body);
    column.append(card);
    return column;
  }

  function render(model) {
    settingsModel = model;
    sectionsRoot.replaceChildren();
    for (const section of model.sections) {
      const fields = model.fields.filter(field => field.section === section.id);
      if (fields.length) sectionsRoot.append(sectionNode(section, fields));
    }
  }

  async function load() {
    const response = await fetch('/api/settings', {cache: 'no-store'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || 'Could not load settings.');
    render(payload);
  }

  async function testService(service, button) {
    button.disabled = true;
    button.className = 'btn btn-outline-secondary btn-sm';
    button.textContent = 'Testing…';
    try {
      const response = await fetch('/api/settings/tests', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({service}),
      });
      const payload = await response.json();
      showTestResult(button, payload.message || 'Connection test complete.', response.ok);
    } catch (_) {
      showTestResult(button, 'The connection test could not be completed.', false);
    }
  }

  async function queueMissingImmichUploads(button) {
    button.disabled = true;
    try {
      const response = await fetch('/api/immich/uploads/missing', {method: 'POST'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || 'Could not queue Immich uploads.');
      showNotice('Missing clips are queued for Immich upload.', 'info');
      while (true) {
        const statusResponse = await fetch(payload.status_url || `/api/jobs/${encodeURIComponent(payload.job_id)}`, {cache: 'no-store'});
        const job = await statusResponse.json();
        if (!statusResponse.ok) throw new Error(job.message || 'Could not read Immich upload progress.');
        if (job.status === 'queued' || job.status === 'running') {
          button.textContent = `Uploading… ${Math.round(Number(job.overall_progress) || 0)}%`;
          await new Promise(resolve => window.setTimeout(resolve, 750));
          continue;
        }
        if (job.status !== 'succeeded') throw new Error((job.error && job.error.message) || job.message || 'Immich bulk upload failed.');
        const result = job.result || {};
        const completed = Number(result.completed) || 0;
        const failed = Number(result.failed) || 0;
        const warningCount = Array.isArray(result.warnings) ? result.warnings.length : 0;
        const summary = `${completed} completed, ${failed} failed` + (warningCount ? `, ${warningCount} with warnings.` : '.');
        showNotice(summary, failed || warningCount ? 'warning' : 'success');
        break;
      }
    } catch (error) {
      showNotice(error.message, 'danger');
    } finally {
      button.disabled = false;
      button.textContent = 'Upload all non-uploaded clips';
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const values = {};
    const clear = [];
    form.querySelectorAll('[data-setting-key]').forEach(control => {
      if (control.disabled) return;
      if (control.dataset.secret === 'true' && !control.value) return;
      values[control.dataset.settingKey] = control.type === 'checkbox' ? String(control.checked) : control.value;
    });
    form.querySelectorAll('[data-clear-key]:checked').forEach(control => {
      delete values[control.dataset.clearKey];
      clear.push(control.dataset.clearKey);
    });
    saveButton.disabled = true;
    try {
      const response = await fetch('/api/settings', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({values, clear}),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || 'Settings could not be saved.');
      render(payload);
      showNotice('Settings saved.');
    } catch (error) {
      showNotice(error.message, 'danger');
    } finally {
      saveButton.disabled = false;
    }
  });

  load().catch(error => showNotice(error.message, 'danger'));
})();
